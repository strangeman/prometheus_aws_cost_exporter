import csv
import io
import json
import os
import tarfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from prometheus_client import Gauge

NEBIUS_ENABLED = os.environ.get("NEBIUS_ENABLED", default=False)
# Service account credentials JSON, as written by
# `nebius iam auth-public-key generate --output <file>`.
NEBIUS_CREDENTIALS = os.environ.get("NEBIUS_CREDENTIALS")
NEBIUS_TENANT_ID = os.environ.get("NEBIUS_TENANT_ID")
NEBIUS_CONTRACT_ID = os.environ.get("NEBIUS_CONTRACT_ID")

EXPORT_POLL_INTERVAL = 10
EXPORT_TIMEOUT = 600

# Cost labels. `nebius_charge` is the FOCUS ChargeDescription: the SKU-level line item
# ("NVIDIA H100 NVLink", "Preemptible NVIDIA H100 NVLink", "Disk (Network SSD)", ...).
# ServiceName alone is nearly useless: Compute is ~99% of the bill.
COST_LABELS = ["nebius_project", "nebius_service", "nebius_charge"]


def _sdk_from_env():
    # Imported here so that importing this module needs neither the SDK's gRPC stack
    # nor credentials (see the import-time side effects note in CLAUDE.md).
    from nebius.base.service_account.credentials_file import ServiceAccountCredentials
    from nebius.base.service_account.service_account import ServiceAccount
    from nebius.base.service_account.static import Reader
    from nebius.sdk import SDK

    creds = ServiceAccountCredentials.from_json(json.loads(NEBIUS_CREDENTIALS)).subject_credentials
    sa = ServiceAccount(creds.parse_private_key(), creds.kid, creds.sub)
    return SDK(credentials=Reader(sa), user_agent_prefix="prometheus-aws-cost-exporter")


def _paginate(call, request_cls, **kwargs):
    token = ""
    while True:
        resp = call(request_cls(page_token=token, **kwargs)).wait()
        yield from resp.items
        token = resp.next_page_token
        if not token:
            return


class Nebius:

    nebius_gpu_spot_price_per_hour = Gauge(
        "nebius_gpu_spot_price_per_hour",
        "Current spot price of one GPU-hour, USD",
        ["region", "platform"])
    nebius_gpu_on_demand_price_per_hour = Gauge(
        "nebius_gpu_on_demand_price_per_hour",
        "On-demand price of one GPU-hour, USD, for platforms that have a spot market",
        ["region", "platform"])
    nebius_preemptible_gpus = Gauge(
        "nebius_preemptible_gpus",
        "GPUs of running preemptible VMs",
        ["region", "platform", "nebius_project"])

    nebius_today_costs = Gauge(
        "nebius_today_costs", "Today (UTC, so far) costs from Nebius, USD", COST_LABELS)
    nebius_yesterday_costs = Gauge(
        "nebius_yesterday_costs", "Yesterday (UTC) costs from Nebius, USD", COST_LABELS)
    nebius_month_to_date_costs = Gauge(
        "nebius_month_to_date_costs", "Month to date costs from Nebius, USD", COST_LABELS)
    nebius_costs_data_end_timestamp_seconds = Gauge(
        "nebius_costs_data_end_timestamp_seconds",
        "End of the latest usage charge period in the billing export")

    nebius_project_scrape_success = Gauge(
        "nebius_project_scrape_success",
        "1 if the last price run could read the project's platforms, instances and prices",
        ["region", "nebius_project"])
    nebius_last_success_timestamp_seconds = Gauge(
        "nebius_last_success_timestamp_seconds",
        "When a Nebius job last completed successfully",
        ["task"])

    def __init__(self, sdk=None):
        if sdk is None and NEBIUS_ENABLED:
            sdk = _sdk_from_env()
        if sdk is None:
            return
        from nebius.api.nebius.billing.v1 import CalculatorServiceClient
        from nebius.api.nebius.billing.v1alpha1 import OneTimeExportServiceClient, SpotPriceServiceClient
        from nebius.api.nebius.compute.v1 import InstanceServiceClient, PlatformServiceClient
        from nebius.api.nebius.iam.v2 import ProjectServiceClient

        self.calculator = CalculatorServiceClient(sdk)
        self.exports = OneTimeExportServiceClient(sdk)
        self.spot_prices = SpotPriceServiceClient(sdk)
        self.instances = InstanceServiceClient(sdk)
        self.platforms = PlatformServiceClient(sdk)
        self.projects = ProjectServiceClient(sdk)

    # ----------------------------------------------------------------------
    # Prices and running preemptible GPUs
    # ----------------------------------------------------------------------

    def get_spot_prices(self):
        """{(region, platform): USD per GPU-hour} for every spot market."""
        from nebius.api.nebius.billing.v1alpha1 import ListSpotPricesRequest

        prices = {}
        for p in _paginate(self.spot_prices.list, ListSpotPricesRequest):
            if p.price.currency.lower() != "usd":
                print(f"{datetime.now()} Nebius: skipping {p.region}/{p.compute_instance.platform} "
                      f"spot price in {p.price.currency}")
                continue
            prices[(p.region, p.compute_instance.platform)] = float(p.price.value)
        return prices

    def get_projects(self):
        """[(project_id, project_name, region)] of the tenant."""
        from nebius.api.nebius.iam.v2 import ListProjectsRequest

        return [(p.metadata.id, p.metadata.name, p.spec.region)
                for p in _paginate(self.projects.list, ListProjectsRequest, parent_id=NEBIUS_TENANT_ID)]

    def get_gpu_presets(self, project_id):
        """{platform: {preset: gpu_count}} for the GPU platforms available in a project."""
        from nebius.api.nebius.compute.v1 import ListPlatformsRequest

        presets = {}
        for p in _paginate(self.platforms.list, ListPlatformsRequest, parent_id=project_id):
            gpus = {s.name: s.resources.gpu_count for s in p.spec.presets if s.resources.gpu_count}
            if gpus:
                presets[p.metadata.name] = gpus
        return presets

    def get_on_demand_prices(self, project_id, presets, platforms):
        """{platform: USD per GPU-hour} for the given platforms of one project.

        Priced on the smallest preset and divided by its GPU count. That is the GPU price
        for platforms billed as one bundled SKU (H100/H200/B200/B300/RTX PRO 6000, i.e.
        everything with a spot market); L40S bills vCPU and RAM separately, but has no
        spot market, so it is never asked for here.
        """
        from nebius.api.google.protobuf import Any
        from nebius.api.nebius.billing.v1 import EstimateBatchRequest, ResourceSpec
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.compute.v1 import CreateInstanceRequest, InstanceSpec, ResourcesSpec

        platforms = sorted(platforms)
        if not platforms:
            return {}
        specs, gpu_counts = [], []
        for platform in platforms:
            preset, gpus = min(presets[platform].items(), key=lambda kv: kv[1])
            req = CreateInstanceRequest(
                metadata=ResourceMetadata(parent_id=project_id),
                spec=InstanceSpec(resources=ResourcesSpec(platform=platform, preset=preset)))
            specs.append(ResourceSpec(spec=Any(
                type_url="type.googleapis.com/nebius.compute.v1.CreateInstanceRequest",
                value=req.SerializeToString())))
            gpu_counts.append(gpus)
        resp = self.calculator.estimate_batch(EstimateBatchRequest(resource_specs=specs)).wait()
        # resource_costs come back in request order
        return {
            platform: float(cost.general.total.cost) / gpus
            for platform, gpus, cost in zip(platforms, gpu_counts, resp.resource_costs)
        }

    def get_preemptible_gpus(self, project_id, presets):
        """{platform: GPUs} of running preemptible VMs in a project."""
        from nebius.api.nebius.compute.v1 import InstanceStatus, ListInstancesRequest

        gpus = defaultdict(int)
        for i in _paginate(self.instances.list, ListInstancesRequest, parent_id=project_id):
            # `spec.preemptible` is an empty message when unset, and empty messages are
            # truthy — presence has to be checked explicitly.
            if not i.spec.check_presence("preemptible"):
                continue
            if i.status.state != InstanceStatus.InstanceState.RUNNING:
                continue
            platform = i.spec.resources.platform
            count = presets.get(platform, {}).get(i.spec.resources.preset, 0)
            if count:
                gpus[platform] += count
        return dict(gpus)

    def fill_price_metrics(self):
        if not NEBIUS_ENABLED:
            print(f"{datetime.now()} Nebius is not enabled.")
            return
        print(f"{datetime.now()} Getting Nebius GPU prices...")
        spot = self.get_spot_prices()
        on_demand, running, scraped = {}, {}, {}
        for project_id, project_name, region in self.get_projects():
            # One unreadable project (e.g. a missing permission) must not cost the
            # metrics of all the others.
            try:
                presets = self.get_gpu_presets(project_id)
                project_running = self.get_preemptible_gpus(project_id, presets)
                # a region may hold several projects; prices are per region, so one is enough
                wanted = {pl for (r, pl) in spot if r == region and pl in presets}
                wanted -= {pl for (r, pl) in on_demand if r == region}
                project_on_demand = self.get_on_demand_prices(project_id, presets, wanted)
            except Exception as e:
                print(f"{datetime.now()} Nebius: skipping project {project_name} ({project_id}, {region}): {e}")
                scraped[(region, project_name)] = 0
                continue
            scraped[(region, project_name)] = 1
            for platform, count in project_running.items():
                running[(region, platform, project_name)] = count
            for platform, price in project_on_demand.items():
                on_demand[(region, platform)] = price

        self.nebius_gpu_spot_price_per_hour.clear()
        for (region, platform), price in spot.items():
            self.nebius_gpu_spot_price_per_hour.labels(region, platform).set(price)
        self.nebius_gpu_on_demand_price_per_hour.clear()
        for (region, platform), price in on_demand.items():
            self.nebius_gpu_on_demand_price_per_hour.labels(region, platform).set(price)
        self.nebius_preemptible_gpus.clear()
        for (region, platform, project), count in running.items():
            self.nebius_preemptible_gpus.labels(region, platform, project).set(count)
        self.nebius_project_scrape_success.clear()
        for (region, project), ok in scraped.items():
            self.nebius_project_scrape_success.labels(region, project).set(ok)
        self.nebius_last_success_timestamp_seconds.labels("prices").set(time.time())
        print(f"{datetime.now()} Finished getting Nebius GPU prices.")

    # ----------------------------------------------------------------------
    # Costs, from the FOCUS 1.2 billing export
    # ----------------------------------------------------------------------

    def export_focus_csv(self, start_period, end_period):
        """Runs a one-time billing export for whole months (`YYYY-MM`) and returns the
        downloaded .tar.gz archive. An export of the current month includes usage up to
        roughly the last hour."""
        from nebius.api.nebius.billing.v1alpha1 import (
            CreateOneTimeExportRequest, ExportFormat, GetOneTimeExportRequest,
            OneTimeExportSpec, OneTimeExportState)
        from nebius.api.nebius.common.v1 import ResourceMetadata

        op = self.exports.create(CreateOneTimeExportRequest(
            metadata=ResourceMetadata(parent_id=NEBIUS_CONTRACT_ID),
            spec=OneTimeExportSpec(
                format=ExportFormat.EXPORT_FORMAT_FOCUS_1_2_CSV,
                start_period=start_period,
                end_period=end_period))).wait()
        deadline = time.monotonic() + EXPORT_TIMEOUT
        pending = (OneTimeExportState.ONE_TIME_EXPORT_STATE_SCHEDULED,
                   OneTimeExportState.ONE_TIME_EXPORT_STATE_RUNNING)
        while True:
            export = self.exports.get(GetOneTimeExportRequest(id=op.resource_id)).wait()
            if export.status.state not in pending:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Nebius export {op.resource_id} not done after {EXPORT_TIMEOUT}s")
            time.sleep(EXPORT_POLL_INTERVAL)
        if export.status.state != OneTimeExportState.ONE_TIME_EXPORT_STATE_SUCCESS:
            raise RuntimeError(f"Nebius export {op.resource_id} ended in {export.status.state}: "
                               f"{export.status.state_details.error}")
        resp = requests.get(export.status.download_url, timeout=300)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def aggregate_costs(archive, today):
        """Sums BilledCost of every CSV in the export archive by COST_LABELS for today,
        yesterday and the month to date (UTC). Returns (today, yesterday, month_to_date,
        data_end), the first three as {labels tuple: USD}; zero sums are dropped."""
        day, yesterday, month = today.isoformat(), (today - timedelta(days=1)).isoformat(), today.strftime("%Y-%m")
        sums = {k: defaultdict(float) for k in ("today", "yesterday", "month")}
        data_end = None
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(".csv"):
                    continue
                with io.TextIOWrapper(tar.extractfile(member), encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        key = tuple(row[c] if row[c] != "NULL" else ""
                                    for c in ("x_ProjectName", "ServiceName", "ChargeDescription"))
                        cost = float(row["BilledCost"])
                        start = row["ChargePeriodStart"]
                        if start[:10] == day:
                            sums["today"][key] += cost
                        elif start[:10] == yesterday:
                            sums["yesterday"][key] += cost
                        if start[:7] == month:
                            sums["month"][key] += cost
                        if row["ChargeCategory"] == "Usage":
                            end = row["ChargePeriodEnd"]
                            if data_end is None or end > data_end:
                                data_end = end

        def nonzero(d):
            return {k: v for k, v in d.items() if round(v, 6) != 0}

        if data_end is not None:
            data_end = datetime.fromisoformat(data_end.replace("Z", "+00:00")).timestamp()
        return nonzero(sums["today"]), nonzero(sums["yesterday"]), nonzero(sums["month"]), data_end

    def fill_cost_metrics(self):
        if not NEBIUS_ENABLED:
            print(f"{datetime.now()} Nebius is not enabled.")
            return
        print(f"{datetime.now()} Calculating Nebius costs...")
        today = datetime.now(timezone.utc).date()
        # on the 1st, yesterday is in the previous billing period
        start_period = (today - timedelta(days=1)).strftime("%Y-%m")
        archive = self.export_focus_csv(start_period, today.strftime("%Y-%m"))
        today_costs, yesterday_costs, month_costs, data_end = self.aggregate_costs(archive, today)

        for gauge, costs in ((self.nebius_today_costs, today_costs),
                             (self.nebius_yesterday_costs, yesterday_costs),
                             (self.nebius_month_to_date_costs, month_costs)):
            gauge.clear()
            for labels, cost in costs.items():
                gauge.labels(*labels).set(cost)
        if data_end is not None:
            self.nebius_costs_data_end_timestamp_seconds.set(data_end)
        self.nebius_last_success_timestamp_seconds.labels("costs").set(time.time())
        print(f"{datetime.now()} Finished calculating Nebius costs.")

"""Savings Plans, Reserved Instances and Spot savings metrics.

Kept apart from `app.aws` because these figures come from Cost Explorer,
which bills $0.01 per request while refreshing its data only once a day.
The module therefore runs on its own, much slower schedule (see
SAVINGS_QUERY_PERIOD in app.py) and leans on the free Describe/Pricing
APIs wherever it can.

All Cost Explorer numbers are month-to-date: the gauges below drop back
down when a new month starts, so query them directly rather than through
rate().
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import Gauge

# Services that can hold reservations, and how to find out whether they
# currently do. The Describe calls are free; the Cost Explorer follow-up is
# not, so it only runs for services that came back with an active reservation.
RESERVATION_PROBES = [
    ("rds", "describe_reserved_db_instances", "ReservedDBInstances",
     "Amazon Relational Database Service"),
    ("ec2", "describe_reserved_instances", "ReservedInstances",
     "Amazon Elastic Compute Cloud - Compute"),
    ("elasticache", "describe_reserved_cache_nodes", "ReservedCacheNodes",
     "Amazon ElastiCache"),
    ("opensearch", "describe_reserved_instances", "ReservedInstances",
     "Amazon OpenSearch Service"),
]


class Savings:
    """Exposes AWS commitment-discount and Spot savings as Prometheus gauges."""

    SAVINGS_ENABLED = os.environ.get("SAVINGS_ENABLED", default=False)
    AWS_REGIONS = os.environ.get(
        "AWS_REGIONS", default="us-east-1,us-east-2,eu-west-1,us-west-1,us-west-2"
    )

    # -- Savings Plans, per plan -------------------------------------------
    savings_plan_info = Gauge(
        "aws_savings_plan_info",
        "Active Savings Plan, always 1. Term and pricing live in the labels.",
        ["savings_plan_id", "type", "family", "region", "payment_option", "state"],
    )
    savings_plan_commitment_hourly_usd = Gauge(
        "aws_savings_plan_commitment_hourly_usd",
        "Hourly commitment of a single Savings Plan.",
        ["savings_plan_id"],
    )
    savings_plan_start_timestamp_seconds = Gauge(
        "aws_savings_plan_start_timestamp_seconds",
        "Unix timestamp the Savings Plan started at.",
        ["savings_plan_id"],
    )
    savings_plan_end_timestamp_seconds = Gauge(
        "aws_savings_plan_end_timestamp_seconds",
        "Unix timestamp the Savings Plan expires at.",
        ["savings_plan_id"],
    )
    savings_plan_utilization_percent = Gauge(
        "aws_savings_plan_utilization_percent",
        "Month-to-date utilization of a single Savings Plan.",
        ["savings_plan_id"],
    )
    savings_plan_unused_commitment_usd = Gauge(
        "aws_savings_plan_unused_commitment_usd",
        "Month-to-date commitment burnt without matching usage, per plan.",
        ["savings_plan_id"],
    )
    savings_plan_net_savings_usd = Gauge(
        "aws_savings_plan_net_savings_usd",
        "Month-to-date saving of a single Savings Plan over on-demand.",
        ["savings_plan_id"],
    )

    # -- Savings Plans, account totals -------------------------------------
    savings_plans_total_commitment_usd = Gauge(
        "aws_savings_plans_total_commitment_usd",
        "Month-to-date committed spend across all Savings Plans.",
    )
    savings_plans_used_commitment_usd = Gauge(
        "aws_savings_plans_used_commitment_usd",
        "Month-to-date commitment matched by usage across all Savings Plans.",
    )
    savings_plans_unused_commitment_usd = Gauge(
        "aws_savings_plans_unused_commitment_usd",
        "Month-to-date commitment paid for but unused across all Savings Plans.",
    )
    savings_plans_utilization_percent = Gauge(
        "aws_savings_plans_utilization_percent",
        "Month-to-date utilization across all Savings Plans.",
    )
    savings_plans_net_savings_usd = Gauge(
        "aws_savings_plans_net_savings_usd",
        "Month-to-date saving of all Savings Plans over on-demand.",
    )
    savings_plans_ondemand_equivalent_usd = Gauge(
        "aws_savings_plans_ondemand_equivalent_usd",
        "What the Savings-Plan-covered usage would have cost at on-demand rates.",
    )
    savings_plans_coverage_percent = Gauge(
        "aws_savings_plans_coverage_percent",
        "Share of Savings-Plan-eligible spend actually covered by a plan.",
    )

    # -- Reserved Instances -------------------------------------------------
    reserved_instances_utilization_percent = Gauge(
        "aws_reserved_instances_utilization_percent",
        "Month-to-date reservation utilization, per service.",
        ["service"],
    )
    reserved_instances_purchased_hours = Gauge(
        "aws_reserved_instances_purchased_hours",
        "Month-to-date reserved hours bought, per service.",
        ["service"],
    )
    reserved_instances_unused_hours = Gauge(
        "aws_reserved_instances_unused_hours",
        "Month-to-date reserved hours paid for but unused, per service.",
        ["service"],
    )
    reserved_instances_net_savings_usd = Gauge(
        "aws_reserved_instances_net_savings_usd",
        "Month-to-date reservation saving over on-demand, per service.",
        ["service"],
    )
    reserved_instances_ondemand_cost_of_hours_used_usd = Gauge(
        "aws_reserved_instances_ondemand_cost_of_hours_used_usd",
        "On-demand cost the used reserved hours would have carried, per service.",
        ["service"],
    )

    # -- RDS reservation inventory -----------------------------------------
    rds_reserved_instance_info = Gauge(
        "aws_rds_reserved_instance_info",
        "Active RDS reserved instance, always 1. Details live in the labels.",
        ["reservation_id", "region", "instance_class", "multi_az", "state",
         "offering_type", "product_description"],
    )
    rds_reserved_instance_count = Gauge(
        "aws_rds_reserved_instance_count",
        "Number of DB instances covered by an RDS reservation.",
        ["reservation_id"],
    )
    rds_reserved_instance_start_timestamp_seconds = Gauge(
        "aws_rds_reserved_instance_start_timestamp_seconds",
        "Unix timestamp the RDS reservation started at.",
        ["reservation_id"],
    )
    rds_reserved_instance_end_timestamp_seconds = Gauge(
        "aws_rds_reserved_instance_end_timestamp_seconds",
        "Unix timestamp the RDS reservation expires at.",
        ["reservation_id"],
    )
    rds_reserved_instance_recurring_hourly_usd = Gauge(
        "aws_rds_reserved_instance_recurring_hourly_usd",
        "Recurring hourly charge of an RDS reservation.",
        ["reservation_id"],
    )

    # -- Spot ---------------------------------------------------------------
    spot_cost_usd = Gauge(
        "aws_spot_cost_usd",
        "Month-to-date spot spend.",
        ["instance_type", "region"],
    )
    spot_usage_hours = Gauge(
        "aws_spot_usage_hours",
        "Month-to-date spot instance-hours.",
        ["instance_type", "region"],
    )
    spot_effective_hourly_usd = Gauge(
        "aws_spot_effective_hourly_usd",
        "Month-to-date average price actually paid per spot instance-hour.",
        ["instance_type", "region"],
    )
    spot_ondemand_hourly_usd = Gauge(
        "aws_spot_ondemand_hourly_usd",
        "On-demand list price for the same instance type, for comparison.",
        ["instance_type", "region"],
    )
    spot_savings_usd = Gauge(
        "aws_spot_savings_usd",
        "Month-to-date spot saving against on-demand list price.",
        ["instance_type", "region"],
    )

    def __init__(self):
        self.ce = boto3.client("ce")
        self.savingsplans = boto3.client("savingsplans")
        # The Pricing API only answers in a handful of regions.
        self.pricing = boto3.client("pricing", region_name="us-east-1")
        self.regions = [r.strip() for r in self.AWS_REGIONS.split(",") if r.strip()]
        self.reservation_probes = list(RESERVATION_PROBES)
        self._clients = {}
        self._price_cache = {}
        self._price_cache_day = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _client(self, service, region):
        key = (service, region)
        if key not in self._clients:
            self._clients[key] = boto3.client(service, region_name=region)
        return self._clients[key]

    @staticmethod
    def _month_to_date():
        """Cost Explorer wants an exclusive end date, so aim at tomorrow."""
        now = datetime.now(timezone.utc)
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return {"Start": start, "End": end}

    @staticmethod
    def _ts(value):
        """Seconds since the epoch from either an ISO string or a datetime."""
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _f(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # Savings Plans
    # ------------------------------------------------------------------

    def get_savings_plans_inventory(self):
        """Term and commitment of every active plan. Free API."""
        r = self.savingsplans.describe_savings_plans(states=["active"])
        plans = []
        for p in r.get("savingsPlans", []):
            plans.append({
                "savings_plan_id": p.get("savingsPlanId", ""),
                "arn": p.get("savingsPlanArn", ""),
                "type": p.get("savingsPlanType", ""),
                "family": p.get("ec2InstanceFamily", ""),
                "region": p.get("region", ""),
                "payment_option": p.get("paymentOption", ""),
                "state": p.get("state", ""),
                "commitment_hourly": self._f(p.get("commitment")),
                "start_ts": self._ts(p["start"]),
                "end_ts": self._ts(p["end"]),
            })
        return plans

    def get_savings_plans_utilization(self):
        """Account-wide month-to-date utilization. One Cost Explorer request."""
        r = self.ce.get_savings_plans_utilization(
            TimePeriod=self._month_to_date(), Granularity="MONTHLY"
        )
        periods = r.get("SavingsPlansUtilizationsByTime", [])
        if not periods:
            return None
        return self._utilization_entry(periods[-1])

    def get_savings_plans_utilization_details(self):
        """Same figures broken down per plan, keyed by plan id."""
        r = self.ce.get_savings_plans_utilization_details(
            TimePeriod=self._month_to_date()
        )
        details = {}
        for entry in r.get("SavingsPlansUtilizationDetails", []):
            arn = entry.get("SavingsPlanArn", "")
            plan_id = arn.rsplit("/", 1)[-1] if arn else ""
            if plan_id:
                details[plan_id] = self._utilization_entry(entry)
        return details

    def _utilization_entry(self, entry):
        u = entry.get("Utilization", {})
        s = entry.get("Savings", {})
        return {
            "total_commitment": self._f(u.get("TotalCommitment")),
            "used_commitment": self._f(u.get("UsedCommitment")),
            "unused_commitment": self._f(u.get("UnusedCommitment")),
            "utilization_percent": self._f(u.get("UtilizationPercentage")),
            "net_savings": self._f(s.get("NetSavings")),
            "ondemand_equivalent": self._f(s.get("OnDemandCostEquivalent")),
        }

    def get_savings_plans_coverage(self):
        """Share of eligible spend a plan actually covers."""
        r = self.ce.get_savings_plans_coverage(
            TimePeriod=self._month_to_date(), Granularity="MONTHLY"
        )
        periods = r.get("SavingsPlansCoverages", [])
        if not periods:
            return None
        return self._f(periods[-1].get("Coverage", {}).get("CoveragePercentage"))

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------

    def get_services_with_reservations(self):
        """Cost Explorer service names that hold at least one active reservation.

        Every entry here becomes a billed Cost Explorer request, so services
        with nothing reserved are dropped before we get that far.
        """
        found = []
        for service, method, key, ce_name in self.reservation_probes:
            if ce_name in found:
                continue
            for region in self.regions:
                try:
                    r = getattr(self._client(service, region), method)()
                except (BotoCoreError, ClientError) as exc:
                    print(f"{datetime.now()} {service} reservations in {region}: {exc}")
                    continue
                if any(i.get("State") == "active" for i in r.get(key, [])):
                    found.append(ce_name)
                    break
        return found

    def get_reservation_utilization(self, service):
        """Month-to-date reservation utilization for one service.

        The SERVICE filter is mandatory in practice: without it Cost Explorer
        answers with an all-zero body rather than an account-wide total.
        """
        r = self.ce.get_reservation_utilization(
            TimePeriod=self._month_to_date(),
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": [service]}},
        )
        periods = r.get("UtilizationsByTime", [])
        if not periods:
            return None
        t = periods[-1].get("Total", {})
        return {
            "utilization_percent": self._f(t.get("UtilizationPercentage")),
            "purchased_hours": self._f(t.get("PurchasedHours")),
            "unused_hours": self._f(t.get("UnusedHours")),
            "net_savings": self._f(t.get("NetRISavings")),
            "ondemand_cost_of_hours_used": self._f(t.get("OnDemandCostOfRIHoursUsed")),
        }

    def get_rds_reservation_inventory(self):
        """Term and pricing of every active RDS reservation. Free API."""
        reservations = []
        for region in self.regions:
            try:
                r = self._client("rds", region).describe_reserved_db_instances()
            except (BotoCoreError, ClientError) as exc:
                print(f"{datetime.now()} rds reservations in {region}: {exc}")
                continue
            for i in r.get("ReservedDBInstances", []):
                if i.get("State") != "active":
                    continue
                start_ts = self._ts(i["StartTime"])
                charges = i.get("RecurringCharges") or [{}]
                reservations.append({
                    "reservation_id": i.get("ReservedDBInstanceId", ""),
                    "region": region,
                    "instance_class": i.get("DBInstanceClass", ""),
                    "multi_az": "true" if i.get("MultiAZ") else "false",
                    "state": i.get("State", ""),
                    "offering_type": i.get("OfferingType", ""),
                    "product_description": i.get("ProductDescription", ""),
                    "count": self._f(i.get("DBInstanceCount")),
                    "start_ts": start_ts,
                    "end_ts": start_ts + self._f(i.get("Duration")),
                    "recurring_hourly": self._f(
                        charges[0].get("RecurringChargeAmount")
                    ),
                })
        return reservations

    # ------------------------------------------------------------------
    # Spot
    # ------------------------------------------------------------------

    def get_spot_costs(self):
        """Month-to-date spot spend per instance type, against on-demand list."""
        r = self.ce.get_cost_and_usage(
            TimePeriod=self._month_to_date(),
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "INSTANCE_TYPE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ],
            Filter={"And": [
                {"Dimensions": {"Key": "SERVICE",
                                "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
                {"Dimensions": {"Key": "PURCHASE_TYPE",
                                "Values": ["Spot Instances"]}},
            ]},
        )
        periods = r.get("ResultsByTime", [])
        if not periods:
            return []

        rows = []
        for g in periods[-1].get("Groups", []):
            instance_type, region = g["Keys"]
            cost = self._f(g["Metrics"]["UnblendedCost"]["Amount"])
            hours = self._f(g["Metrics"]["UsageQuantity"]["Amount"])
            if hours <= 0:
                continue
            ondemand = self.get_ondemand_hourly(instance_type, region) or 0.0
            rows.append({
                "instance_type": instance_type,
                "region": region,
                "cost": cost,
                "hours": hours,
                "effective_hourly": cost / hours,
                "ondemand_hourly": ondemand,
                "savings": hours * ondemand - cost if ondemand else 0.0,
            })
        return rows

    def get_ondemand_hourly(self, instance_type, region):
        """On-demand list price, cached for a day. The Pricing API is free."""
        today = datetime.now(timezone.utc).date()
        if self._price_cache_day != today:
            self._price_cache = {}
            self._price_cache_day = today

        key = (instance_type, region)
        if key in self._price_cache:
            return self._price_cache[key]

        price = None
        try:
            r = self.pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=1,
            )
            product = json.loads(r["PriceList"][0])
            term = next(iter(product["terms"]["OnDemand"].values()))
            dimension = next(iter(term["priceDimensions"].values()))
            price = float(dimension["pricePerUnit"]["USD"])
        except (BotoCoreError, ClientError, KeyError, IndexError, ValueError,
                StopIteration) as exc:
            print(f"{datetime.now()} on-demand price for {instance_type}/{region}: {exc}")

        self._price_cache[key] = price
        return price

    # ------------------------------------------------------------------
    # metric collection
    # ------------------------------------------------------------------

    def fill_metrics(self):
        if not self.SAVINGS_ENABLED:
            print(f"{datetime.now()} Savings metrics are not enabled.")
            return

        print(f"{datetime.now()} Collecting AWS savings metrics...")
        self._fill_savings_plans()
        self._fill_reservations()
        self._fill_spot()
        print(f"{datetime.now()} Finished collecting AWS savings metrics.")

    def _fill_savings_plans(self):
        try:
            plans = self.get_savings_plans_inventory()
            details = self.get_savings_plans_utilization_details()
            totals = self.get_savings_plans_utilization()
            coverage = self.get_savings_plans_coverage()
        except (BotoCoreError, ClientError) as exc:
            print(f"{datetime.now()} savings plans: {exc}")
            return

        # Retired plans must not linger as stale series.
        self.savings_plan_info.clear()
        self.savings_plan_commitment_hourly_usd.clear()
        self.savings_plan_start_timestamp_seconds.clear()
        self.savings_plan_end_timestamp_seconds.clear()
        self.savings_plan_utilization_percent.clear()
        self.savings_plan_unused_commitment_usd.clear()
        self.savings_plan_net_savings_usd.clear()

        for p in plans:
            plan_id = p["savings_plan_id"]
            self.savings_plan_info.labels(
                plan_id, p["type"], p["family"], p["region"],
                p["payment_option"], p["state"],
            ).set(1)
            self.savings_plan_commitment_hourly_usd.labels(plan_id).set(
                p["commitment_hourly"])
            self.savings_plan_start_timestamp_seconds.labels(plan_id).set(p["start_ts"])
            self.savings_plan_end_timestamp_seconds.labels(plan_id).set(p["end_ts"])

            d = details.get(plan_id)
            if d:
                self.savings_plan_utilization_percent.labels(plan_id).set(
                    d["utilization_percent"])
                self.savings_plan_unused_commitment_usd.labels(plan_id).set(
                    d["unused_commitment"])
                self.savings_plan_net_savings_usd.labels(plan_id).set(d["net_savings"])

        if totals:
            self.savings_plans_total_commitment_usd.set(totals["total_commitment"])
            self.savings_plans_used_commitment_usd.set(totals["used_commitment"])
            self.savings_plans_unused_commitment_usd.set(totals["unused_commitment"])
            self.savings_plans_utilization_percent.set(totals["utilization_percent"])
            self.savings_plans_net_savings_usd.set(totals["net_savings"])
            self.savings_plans_ondemand_equivalent_usd.set(totals["ondemand_equivalent"])
        if coverage is not None:
            self.savings_plans_coverage_percent.set(coverage)

    def _fill_reservations(self):
        try:
            services = self.get_services_with_reservations()
        except (BotoCoreError, ClientError) as exc:
            print(f"{datetime.now()} reservation discovery: {exc}")
            return

        self.reserved_instances_utilization_percent.clear()
        self.reserved_instances_purchased_hours.clear()
        self.reserved_instances_unused_hours.clear()
        self.reserved_instances_net_savings_usd.clear()
        self.reserved_instances_ondemand_cost_of_hours_used_usd.clear()

        for service in services:
            try:
                u = self.get_reservation_utilization(service)
            except (BotoCoreError, ClientError) as exc:
                print(f"{datetime.now()} reservation utilization for {service}: {exc}")
                continue
            if not u:
                continue
            self.reserved_instances_utilization_percent.labels(service).set(
                u["utilization_percent"])
            self.reserved_instances_purchased_hours.labels(service).set(
                u["purchased_hours"])
            self.reserved_instances_unused_hours.labels(service).set(u["unused_hours"])
            self.reserved_instances_net_savings_usd.labels(service).set(u["net_savings"])
            self.reserved_instances_ondemand_cost_of_hours_used_usd.labels(service).set(
                u["ondemand_cost_of_hours_used"])

        try:
            reservations = self.get_rds_reservation_inventory()
        except (BotoCoreError, ClientError) as exc:
            print(f"{datetime.now()} rds reservation inventory: {exc}")
            return

        self.rds_reserved_instance_info.clear()
        self.rds_reserved_instance_count.clear()
        self.rds_reserved_instance_start_timestamp_seconds.clear()
        self.rds_reserved_instance_end_timestamp_seconds.clear()
        self.rds_reserved_instance_recurring_hourly_usd.clear()

        for r in reservations:
            rid = r["reservation_id"]
            self.rds_reserved_instance_info.labels(
                rid, r["region"], r["instance_class"], r["multi_az"],
                r["state"], r["offering_type"], r["product_description"],
            ).set(1)
            self.rds_reserved_instance_count.labels(rid).set(r["count"])
            self.rds_reserved_instance_start_timestamp_seconds.labels(rid).set(
                r["start_ts"])
            self.rds_reserved_instance_end_timestamp_seconds.labels(rid).set(r["end_ts"])
            self.rds_reserved_instance_recurring_hourly_usd.labels(rid).set(
                r["recurring_hourly"])

    def _fill_spot(self):
        try:
            rows = self.get_spot_costs()
        except (BotoCoreError, ClientError) as exc:
            print(f"{datetime.now()} spot costs: {exc}")
            return

        # Instance types we stopped running this month must not stick around.
        self.spot_cost_usd.clear()
        self.spot_usage_hours.clear()
        self.spot_effective_hourly_usd.clear()
        self.spot_ondemand_hourly_usd.clear()
        self.spot_savings_usd.clear()

        for r in rows:
            labels = (r["instance_type"], r["region"])
            self.spot_cost_usd.labels(*labels).set(r["cost"])
            self.spot_usage_hours.labels(*labels).set(r["hours"])
            self.spot_effective_hourly_usd.labels(*labels).set(r["effective_hourly"])
            self.spot_ondemand_hourly_usd.labels(*labels).set(r["ondemand_hourly"])
            self.spot_savings_usd.labels(*labels).set(r["savings"])

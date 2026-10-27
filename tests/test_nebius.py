import csv
import io
import tarfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.compute.v1 import (
    Instance, InstanceSpec, InstanceStatus, PreemptibleSpec, ResourcesSpec)
from prometheus_client import REGISTRY

import app.nebius as nebius_module
from app.nebius import Nebius

FOCUS_COLUMNS = [
    "BilledCost", "ChargeCategory", "ChargeDescription", "ChargePeriodEnd", "ChargePeriodStart",
    "ServiceName", "x_ProjectName",
]


def _archive(rows, name="2026-10.csv"):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FOCUS_COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    data = buf.getvalue().encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _row(cost, start, end, charge="NVIDIA H100 NVLink", project="default-project",
         service="Compute", category="Usage"):
    return {
        "BilledCost": cost, "ChargeCategory": category, "ChargeDescription": charge,
        "ChargePeriodStart": start, "ChargePeriodEnd": end,
        "ServiceName": service, "x_ProjectName": project,
    }


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(nebius_module, "NEBIUS_ENABLED", "true")
    monkeypatch.setattr(nebius_module, "NEBIUS_TENANT_ID", "tenant-1")
    monkeypatch.setattr(nebius_module, "NEBIUS_CONTRACT_ID", "contract-1")
    # no SDK: every test replaces the API calls it needs
    monkeypatch.setattr(nebius_module, "_sdk_from_env", lambda: None)


# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------

def test_aggregate_costs_splits_today_yesterday_and_month():
    archive = _archive([
        _row("1.5", "2026-10-27T00:00:00Z", "2026-10-27T01:00:00Z"),
        _row("2.5", "2026-10-27T05:00:00Z", "2026-10-27T06:00:00Z"),
        _row("4", "2026-10-26T23:00:00Z", "2026-10-27T00:00:00Z"),
        _row("8", "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z"),
        # previous month: only ever "yesterday" on the 1st, never month to date
        _row("100", "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z"),
    ])

    today, yesterday, month, data_end = Nebius.aggregate_costs(archive, date(2026, 10, 27))

    key = ("default-project", "Compute", "NVIDIA H100 NVLink")
    assert today == {key: pytest.approx(4.0)}
    assert yesterday == {key: pytest.approx(4.0)}
    assert month == {key: pytest.approx(16.0)}
    assert data_end == datetime(2026, 10, 27, 6, tzinfo=timezone.utc).timestamp()


def test_aggregate_costs_on_first_of_month_takes_yesterday_from_previous_period():
    archive = _archive([
        _row("3", "2026-10-31T10:00:00Z", "2026-10-31T11:00:00Z"),
        _row("1", "2026-11-01T00:00:00Z", "2026-11-01T01:00:00Z"),
    ])

    today, yesterday, month, _ = Nebius.aggregate_costs(archive, date(2026, 11, 1))

    key = ("default-project", "Compute", "NVIDIA H100 NVLink")
    assert yesterday == {key: pytest.approx(3.0)}
    assert today == month == {key: pytest.approx(1.0)}


def test_aggregate_costs_keys_by_project_service_and_charge_and_drops_zeros():
    archive = _archive([
        _row("1", "2026-10-27T00:00:00Z", "2026-10-27T01:00:00Z", charge="Disk (Network SSD)"),
        _row("2", "2026-10-27T00:00:00Z", "2026-10-27T01:00:00Z", project="default-us-central1"),
        _row("0.000", "2026-10-27T00:00:00Z", "2026-10-27T01:00:00Z", service="Virtual Networks",
             charge="Public IP"),
        _row("0", "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", category="Tax",
             service="Billing", project="NULL", charge="Tax"),
    ])

    today, _, month, data_end = Nebius.aggregate_costs(archive, date(2026, 10, 27))

    assert today == {
        ("default-project", "Compute", "Disk (Network SSD)"): pytest.approx(1.0),
        ("default-us-central1", "Compute", "NVIDIA H100 NVLink"): pytest.approx(2.0),
    }
    assert month == today
    # the month-long Tax row must not pass for fresh usage data
    assert data_end == datetime(2026, 10, 27, 1, tzinfo=timezone.utc).timestamp()


def test_fill_cost_metrics_exports_from_yesterdays_period(enabled, monkeypatch):
    n = Nebius()
    n.export_focus_csv = MagicMock(return_value=_archive([
        _row("5", "2026-10-31T10:00:00Z", "2026-10-31T11:00:00Z"),
    ]))

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 1, 3, tzinfo=tz)

    monkeypatch.setattr(nebius_module, "datetime", FakeDatetime)
    n.fill_cost_metrics()

    n.export_focus_csv.assert_called_once_with("2026-10", "2026-11")
    assert _value("nebius_yesterday_costs", nebius_project="default-project",
                  nebius_service="Compute", nebius_charge="NVIDIA H100 NVLink") == 5.0
    assert _value("nebius_last_success_timestamp_seconds", task="costs") is not None


# --------------------------------------------------------------------------
# Prices and running preemptible GPUs
# --------------------------------------------------------------------------

def _instance(platform, preset, state, preemptible=True):
    spec = InstanceSpec(resources=ResourcesSpec(platform=platform, preset=preset))
    if preemptible:
        spec.preemptible = PreemptibleSpec(on_preemption=PreemptibleSpec.PreemptionPolicy.STOP)
    return Instance(metadata=ResourceMetadata(id="i"), spec=spec, status=InstanceStatus(state=state))


def test_preemptible_gpus_count_only_running_preemptible_vms():
    n = Nebius()
    n.instances = MagicMock()
    running = InstanceStatus.InstanceState.RUNNING
    n.instances.list.return_value.wait.return_value = SimpleNamespace(next_page_token="", items=[
        _instance("gpu-h100-sxm", "8gpu-128vcpu-1600gb", running),
        _instance("gpu-h100-sxm", "1gpu-16vcpu-200gb", running),
        _instance("gpu-h100-sxm", "1gpu-16vcpu-200gb", InstanceStatus.InstanceState.STOPPED),
        _instance("gpu-h100-sxm", "1gpu-16vcpu-200gb", running, preemptible=False),
        _instance("cpu-e2", "4vcpu-16gb", running),
    ])
    presets = {"gpu-h100-sxm": {"1gpu-16vcpu-200gb": 1, "8gpu-128vcpu-1600gb": 8}}

    assert n.get_preemptible_gpus("project-1", presets) == {"gpu-h100-sxm": 9}


def test_on_demand_prices_are_per_gpu_of_the_smallest_preset():
    n = Nebius()
    n.calculator = MagicMock()
    cost = lambda c: SimpleNamespace(general=SimpleNamespace(total=SimpleNamespace(cost=c)))
    n.calculator.estimate_batch.return_value.wait.return_value = SimpleNamespace(
        resource_costs=[cost("4.5"), cost("8.5")])
    presets = {
        "gpu-h100-sxm": {"8gpu-128vcpu-1600gb": 8, "1gpu-16vcpu-200gb": 1},
        "gpu-b200-sxm": {"1gpu-20vcpu-224gb": 1},
    }

    prices = n.get_on_demand_prices("project-1", presets, {"gpu-h100-sxm", "gpu-b200-sxm"})

    # sorted request order: b200, h100
    assert prices == {"gpu-b200-sxm": 4.5, "gpu-h100-sxm": 8.5}
    request = n.calculator.estimate_batch.call_args.args[0]
    assert len(request.resource_specs) == 2


def test_fill_price_metrics_prices_each_region_once(enabled):
    n = Nebius()
    n.get_spot_prices = MagicMock(return_value={
        ("eu-north1", "gpu-h100-sxm"): 4.6,
        ("us-central1", "gpu-h200-sxm"): 1.1,
    })
    n.get_projects = MagicMock(return_value=[
        ("project-a", "default-project", "eu-north1"),
        ("project-b", "second-eu-north1", "eu-north1"),
        ("project-c", "default-us-central1", "us-central1"),
    ])
    n.get_gpu_presets = MagicMock(return_value={
        "gpu-h100-sxm": {"1gpu-16vcpu-200gb": 1}, "gpu-h200-sxm": {"1gpu-16vcpu-200gb": 1}})
    n.get_preemptible_gpus = MagicMock(side_effect=[{"gpu-h100-sxm": 2}, {}, {}])
    n.get_on_demand_prices = MagicMock(side_effect=lambda project, presets, platforms: {
        p: {"gpu-h100-sxm": 4.5, "gpu-h200-sxm": 5.4}[p] for p in platforms})

    n.fill_price_metrics()

    asked = [c.args[2] for c in n.get_on_demand_prices.call_args_list]
    assert asked == [{"gpu-h100-sxm"}, set(), {"gpu-h200-sxm"}]
    labels = dict(region="eu-north1", platform="gpu-h100-sxm")
    assert _value("nebius_gpu_spot_price_per_hour", **labels) == 4.6
    assert _value("nebius_gpu_on_demand_price_per_hour", **labels) == 4.5
    assert _value("nebius_preemptible_gpus", nebius_project="default-project", **labels) == 2

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

os.environ["SAVINGS_ENABLED"] = "true"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from app.savings import RESERVATION_PROBES, Savings  # noqa: E402


def _dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


@pytest.fixture
def savings():
    s = Savings()
    s.ce = MagicMock()
    s.savingsplans = MagicMock()
    s.pricing = MagicMock()
    s._clients = {}
    return s


# --------------------------------------------------------------------------
# Savings Plans inventory
# --------------------------------------------------------------------------

def test_savings_plan_inventory_reports_term_and_commitment(savings):
    savings.savingsplans.describe_savings_plans.return_value = {
        "savingsPlans": [
            {
                "savingsPlanId": "6abbfae1",
                "savingsPlanArn": "arn:aws:savingsplans::1:savingsplan/6abbfae1",
                "description": "1 year No Upfront m7g EC2 Instance Savings Plan in us-east-2",
                "savingsPlanType": "EC2Instance",
                "ec2InstanceFamily": "m7g",
                "region": "us-east-2",
                "paymentOption": "No Upfront",
                "state": "active",
                "commitment": "0.89300000",
                "start": "2026-07-27T10:59:57.975Z",
                "end": "2027-07-27T10:59:56.975Z",
            }
        ]
    }

    plans = savings.get_savings_plans_inventory()

    assert len(plans) == 1
    p = plans[0]
    assert p["savings_plan_id"] == "6abbfae1"
    assert p["family"] == "m7g"
    assert p["commitment_hourly"] == pytest.approx(0.893)
    assert p["start_ts"] == _dt("2026-07-27T10:59:57.975").timestamp()
    assert p["end_ts"] == _dt("2027-07-27T10:59:56.975").timestamp()


def test_savings_plan_inventory_asks_only_for_active_plans(savings):
    savings.savingsplans.describe_savings_plans.return_value = {"savingsPlans": []}

    savings.get_savings_plans_inventory()

    kwargs = savings.savingsplans.describe_savings_plans.call_args.kwargs
    assert kwargs["states"] == ["active"]


def test_savings_plan_inventory_tolerates_missing_ec2_family(savings):
    """Compute SP (not EC2Instance SP) carries no ec2InstanceFamily / region."""
    savings.savingsplans.describe_savings_plans.return_value = {
        "savingsPlans": [
            {
                "savingsPlanId": "abc",
                "savingsPlanArn": "arn:x",
                "savingsPlanType": "Compute",
                "paymentOption": "No Upfront",
                "state": "active",
                "commitment": "1.5",
                "start": "2026-01-01T00:00:00.000Z",
                "end": "2027-01-01T00:00:00.000Z",
            }
        ]
    }

    p = savings.get_savings_plans_inventory()[0]

    assert p["family"] == ""
    assert p["region"] == ""


# --------------------------------------------------------------------------
# Savings Plans utilization
# --------------------------------------------------------------------------

def test_savings_plans_utilization_takes_the_latest_period(savings):
    """CE returns one entry per month; month-to-date is the last one."""
    savings.ce.get_savings_plans_utilization.return_value = {
        "SavingsPlansUtilizationsByTime": [
            {
                "TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
                "Utilization": {
                    "TotalCommitment": "589.63",
                    "UsedCommitment": "462.69",
                    "UnusedCommitment": "126.94",
                    "UtilizationPercentage": "78.47",
                },
                "Savings": {"NetSavings": "128.46", "OnDemandCostEquivalent": "718.10"},
            },
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-27"},
                "Utilization": {
                    "TotalCommitment": "2227.42",
                    "UsedCommitment": "2102.90",
                    "UnusedCommitment": "124.52",
                    "UtilizationPercentage": "94.40",
                },
                "Savings": {"NetSavings": "1013.88", "OnDemandCostEquivalent": "3241.30"},
            },
        ]
    }

    u = savings.get_savings_plans_utilization()

    assert u["utilization_percent"] == pytest.approx(94.40)
    assert u["unused_commitment"] == pytest.approx(124.52)
    assert u["net_savings"] == pytest.approx(1013.88)


def test_savings_plans_utilization_handles_empty_response(savings):
    savings.ce.get_savings_plans_utilization.return_value = {
        "SavingsPlansUtilizationsByTime": []
    }

    assert savings.get_savings_plans_utilization() is None


def test_savings_plans_utilization_details_keyed_by_plan_id(savings):
    savings.ce.get_savings_plans_utilization_details.return_value = {
        "SavingsPlansUtilizationDetails": [
            {
                "SavingsPlanArn": "arn:aws:savingsplans::1:savingsplan/6abbfae1",
                "Utilization": {
                    "TotalCommitment": "100.0",
                    "UsedCommitment": "90.0",
                    "UnusedCommitment": "10.0",
                    "UtilizationPercentage": "90.0",
                },
                "Savings": {"NetSavings": "25.0", "OnDemandCostEquivalent": "115.0"},
            }
        ]
    }

    details = savings.get_savings_plans_utilization_details()

    assert "6abbfae1" in details
    assert details["6abbfae1"]["utilization_percent"] == pytest.approx(90.0)
    assert details["6abbfae1"]["net_savings"] == pytest.approx(25.0)


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------

def test_reservation_utilization_always_filters_by_service(savings):
    """Regression: CE returns an all-zero body when no SERVICE filter is given."""
    savings.ce.get_reservation_utilization.return_value = {
        "UtilizationsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-27"},
                "Total": {
                    "UtilizationPercentage": "100",
                    "PurchasedHours": "1218",
                    "UnusedHours": "0",
                    "NetRISavings": "161.56",
                    "OnDemandCostOfRIHoursUsed": "501.01",
                },
            }
        ]
    }

    u = savings.get_reservation_utilization("Amazon Relational Database Service")

    kwargs = savings.ce.get_reservation_utilization.call_args.kwargs
    assert kwargs["Filter"] == {
        "Dimensions": {
            "Key": "SERVICE",
            "Values": ["Amazon Relational Database Service"],
        }
    }
    assert u["utilization_percent"] == pytest.approx(100.0)
    assert u["net_savings"] == pytest.approx(161.56)
    assert u["purchased_hours"] == pytest.approx(1218.0)


def test_reservation_utilization_handles_empty_response(savings):
    savings.ce.get_reservation_utilization.return_value = {"UtilizationsByTime": []}

    assert savings.get_reservation_utilization("Amazon ElastiCache") is None


def test_services_with_reservations_skips_services_without_active_ones(savings):
    """CE is billed per request, so only query services that actually hold reservations."""
    rds, ec2 = MagicMock(), MagicMock()
    rds.describe_reserved_db_instances.return_value = {
        "ReservedDBInstances": [{"State": "active"}, {"State": "retired"}]
    }
    ec2.describe_reserved_instances.return_value = {
        "ReservedInstances": [{"State": "retired"}]
    }
    savings._clients = {("rds", "us-east-2"): rds, ("ec2", "us-east-2"): ec2}
    savings.regions = ["us-east-2"]
    savings.reservation_probes = [
        p for p in RESERVATION_PROBES if p[0] in ("rds", "ec2")
    ]

    services = savings.get_services_with_reservations()

    assert services == ["Amazon Relational Database Service"]


def test_reservation_probes_cover_every_reservable_service():
    assert [p[3] for p in RESERVATION_PROBES] == [
        "Amazon Relational Database Service",
        "Amazon Elastic Compute Cloud - Compute",
        "Amazon ElastiCache",
        "Amazon OpenSearch Service",
    ]


def test_services_with_reservations_survives_a_denied_region(savings):
    """A missing permission in one region must not sink the whole probe."""
    from botocore.exceptions import ClientError

    denied, allowed = MagicMock(), MagicMock()
    denied.describe_reserved_db_instances.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}},
        "DescribeReservedDBInstances",
    )
    allowed.describe_reserved_db_instances.return_value = {
        "ReservedDBInstances": [{"State": "active"}]
    }
    savings._clients = {("rds", "eu-west-1"): denied, ("rds", "us-east-2"): allowed}
    savings.regions = ["eu-west-1", "us-east-2"]
    savings.reservation_probes = [p for p in RESERVATION_PROBES if p[0] == "rds"]

    assert savings.get_services_with_reservations() == [
        "Amazon Relational Database Service"
    ]


def test_rds_reservation_inventory_derives_end_from_duration(savings):
    rds = MagicMock()
    rds.describe_reserved_db_instances.return_value = {
        "ReservedDBInstances": [
            {
                "ReservedDBInstanceId": "rds-prod-2026",
                "DBInstanceClass": "db.m8g.xlarge",
                "DBInstanceCount": 2,
                "MultiAZ": True,
                "State": "active",
                "OfferingType": "No Upfront",
                "ProductDescription": "postgresql",
                "StartTime": _dt("2026-08-14T11:03:18.622"),
                "Duration": 31536000,
                "FixedPrice": 0.0,
                "RecurringCharges": [{"RecurringChargeAmount": 0.45}],
            },
            {"ReservedDBInstanceId": "old", "State": "retired", "Duration": 1},
        ]
    }
    savings._clients = {("rds", "us-east-2"): rds}
    savings.regions = ["us-east-2"]

    res = savings.get_rds_reservation_inventory()

    assert len(res) == 1
    r = res[0]
    assert r["reservation_id"] == "rds-prod-2026"
    assert r["count"] == 2
    assert r["multi_az"] == "true"
    assert r["recurring_hourly"] == pytest.approx(0.45)
    assert r["end_ts"] == pytest.approx(r["start_ts"] + 31536000)


# --------------------------------------------------------------------------
# Spot
# --------------------------------------------------------------------------

def test_spot_costs_compute_effective_and_ondemand_savings(savings):
    savings.ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-27"},
                "Groups": [
                    {
                        "Keys": ["m8g.xlarge", "us-east-2"],
                        "Metrics": {
                            "UnblendedCost": {"Amount": "69.20"},
                            "UsageQuantity": {"Amount": "987.42"},
                        },
                    }
                ],
            }
        ]
    }
    savings.get_ondemand_hourly = MagicMock(return_value=0.17952)

    rows = savings.get_spot_costs()

    assert len(rows) == 1
    r = rows[0]
    assert r["instance_type"] == "m8g.xlarge"
    assert r["region"] == "us-east-2"
    assert r["effective_hourly"] == pytest.approx(69.20 / 987.42)
    assert r["ondemand_hourly"] == pytest.approx(0.17952)
    assert r["savings"] == pytest.approx(987.42 * 0.17952 - 69.20)


def test_spot_costs_skip_groups_with_no_usage(savings):
    """Zero hours would make the effective-price division blow up."""
    savings.ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-27"},
                "Groups": [
                    {
                        "Keys": ["m8g.xlarge", "us-east-2"],
                        "Metrics": {
                            "UnblendedCost": {"Amount": "0"},
                            "UsageQuantity": {"Amount": "0"},
                        },
                    }
                ],
            }
        ]
    }
    savings.get_ondemand_hourly = MagicMock(return_value=0.17952)

    assert savings.get_spot_costs() == []


def test_spot_costs_reports_zero_savings_when_price_lookup_fails(savings):
    savings.ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-27"},
                "Groups": [
                    {
                        "Keys": ["exotic.type", "us-east-2"],
                        "Metrics": {
                            "UnblendedCost": {"Amount": "10.0"},
                            "UsageQuantity": {"Amount": "100.0"},
                        },
                    }
                ],
            }
        ]
    }
    savings.get_ondemand_hourly = MagicMock(return_value=None)

    r = savings.get_spot_costs()[0]

    assert r["ondemand_hourly"] == 0.0
    assert r["savings"] == 0.0


def test_spot_costs_filter_pins_purchase_type_to_spot(savings):
    savings.ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

    savings.get_spot_costs()

    kwargs = savings.ce.get_cost_and_usage.call_args.kwargs
    dims = [d["Dimensions"] for d in kwargs["Filter"]["And"]]
    assert {"Key": "PURCHASE_TYPE", "Values": ["Spot Instances"]} in dims
    assert [g["Key"] for g in kwargs["GroupBy"]] == ["INSTANCE_TYPE", "REGION"]


# --------------------------------------------------------------------------
# On-demand pricing
# --------------------------------------------------------------------------

def test_ondemand_price_is_parsed_and_cached(savings):
    savings.pricing.get_products.return_value = {
        "PriceList": [
            '{"terms": {"OnDemand": {"x": {"priceDimensions": {"y": '
            '{"pricePerUnit": {"USD": "0.1795200000"}}}}}}}'
        ]
    }

    first = savings.get_ondemand_hourly("m8g.xlarge", "us-east-2")
    second = savings.get_ondemand_hourly("m8g.xlarge", "us-east-2")

    assert first == pytest.approx(0.17952)
    assert second == pytest.approx(0.17952)
    assert savings.pricing.get_products.call_count == 1


def test_ondemand_price_returns_none_when_not_found(savings):
    savings.pricing.get_products.return_value = {"PriceList": []}

    assert savings.get_ondemand_hourly("nope.xlarge", "us-east-2") is None

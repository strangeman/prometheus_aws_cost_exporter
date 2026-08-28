# Prometheus Cost Exporter

## Intro

Are you looking for some system that alerts when your today's spending on cloud providers exceeds some limit?  That's just what this exporter is made for.

The exporter is a Python server that connects to AWS Cost Explorer and GCP BigQuery with a customizable period, and exposes last responses as Prometheus metrics.

Alongside raw spend it can also report on **commitment discounts and spot savings**: Savings Plan and Reserved Instance utilization, and what spot actually saves against on-demand list price. See [Savings metrics](#savings-metrics).

## Configuration

Configuration is made through environment variables:

| Environment variable        | Description           | Default  |
| ------------- |:-------------:| -----:|
| QUERY_PERIOD      | Period to update metrics, querying AWS Cost Explorer API (0.01$ per request) | 1800 |
| AWS_ENABLED | Enable AWS metrics gathering      |   False |
| GCP_ENABLED | Enable GCP metrics gathering      |   False |
| GCP_BQ_BILLING_PROJECT | Name of GCP project with BQ billing dataset      |   False |
| GCP_BQ_DATASET_ID | Name of billing dataset in BQ      |   False |
| SERVERSCOM_ENABLED | Enable Servers.com metrics gathering      |   False |
| SERVERSCOM_TOKEN | Servers.com API token      |   None |
| SAVINGS_ENABLED | Enable Savings Plan / Reserved Instance / spot savings metrics |   False |
| SAVINGS_QUERY_PERIOD | Period to update the savings metrics, separate from `QUERY_PERIOD` |   21600 |
| AWS_REGIONS | Regions scanned for reservations, comma-separated |   us-east-1,us-east-2,eu-west-1,us-west-1,us-west-2 |

`SAVINGS_QUERY_PERIOD` is deliberately separate. Cost Explorer bills $0.01 per request and refreshes its data roughly once a day, so a 30-minute loop buys no freshness and costs real money — five calls on a 1800s period run to about $70/month on their own.

## Quickstart

### AWS IAM permissions

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "VisualEditor0",
            "Effect": "Allow",
            "Action": "ce:*",
            "Resource": "*"
        }
    ]
}
```

With `SAVINGS_ENABLED=true` the exporter also needs the free inventory and
pricing reads:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ReservationAndSavingsPlanInventory",
            "Effect": "Allow",
            "Action": [
                "savingsplans:DescribeSavingsPlans",
                "rds:DescribeReservedDBInstances",
                "ec2:DescribeReservedInstances",
                "elasticache:DescribeReservedCacheNodes",
                "es:DescribeReservedInstances"
            ],
            "Resource": "*"
        },
        {
            "Sid": "OnDemandListPriceLookup",
            "Effect": "Allow",
            "Action": ["pricing:GetProducts", "pricing:DescribeServices"],
            "Resource": "*"
        }
    ]
}
```


## Savings metrics

Enabled with `SAVINGS_ENABLED=true`. Implemented in `app/savings.py`, scheduled
independently of the cost metrics.

**Everything from Cost Explorer here is month-to-date.** The gauges climb through
the month and drop back on the 1st, so read them directly — never through
`rate()` or `increase()`.

### Savings Plans

Per plan (label `savings_plan_id`), from the free `savingsplans:DescribeSavingsPlans`:

| Metric | Meaning |
| --- | --- |
| `aws_savings_plan_info` | always 1; `type`, `family`, `region`, `payment_option`, `state` in labels |
| `aws_savings_plan_commitment_hourly_usd` | hourly commitment |
| `aws_savings_plan_start_timestamp_seconds` | when the term began |
| `aws_savings_plan_end_timestamp_seconds` | when it expires |
| `aws_savings_plan_utilization_percent` | month-to-date utilization of this plan |
| `aws_savings_plan_unused_commitment_usd` | commitment this plan burnt unused |
| `aws_savings_plan_net_savings_usd` | what this plan saved over on-demand |

Account-wide (no labels), from `ce:GetSavingsPlansUtilization` and
`ce:GetSavingsPlansCoverage`:

`aws_savings_plans_total_commitment_usd`, `aws_savings_plans_used_commitment_usd`,
`aws_savings_plans_unused_commitment_usd`, `aws_savings_plans_utilization_percent`,
`aws_savings_plans_net_savings_usd`, `aws_savings_plans_ondemand_equivalent_usd`,
`aws_savings_plans_coverage_percent`.

Note the singular/plural split: `aws_savings_plan_*` is per plan and always
carries `savings_plan_id`; `aws_savings_plans_*` is the account total.

### Reserved Instances

Labelled by `service`, from `ce:GetReservationUtilization`:
`aws_reserved_instances_utilization_percent`,
`aws_reserved_instances_purchased_hours`, `aws_reserved_instances_unused_hours`,
`aws_reserved_instances_net_savings_usd`,
`aws_reserved_instances_ondemand_cost_of_hours_used_usd`.

Only services that currently hold an active reservation are queried at all. The
check runs over free `Describe*` calls first, because each Cost Explorer request
is billable.

`GetReservationUtilization` **must** be given a `SERVICE` filter. Called without
one it does not return an account-wide total — it answers with an all-zero body,
which reads exactly like "no reservations exist".

RDS reservation terms come from the free `rds:DescribeReservedDBInstances`
(label `reservation_id`): `aws_rds_reserved_instance_info`,
`aws_rds_reserved_instance_count`,
`aws_rds_reserved_instance_start_timestamp_seconds`,
`aws_rds_reserved_instance_end_timestamp_seconds`,
`aws_rds_reserved_instance_recurring_hourly_usd`.

### Spot

Labelled by `instance_type` and `region`. Spend and hours come from
`ce:GetCostAndUsage` filtered to `PURCHASE_TYPE=Spot Instances`; the on-demand
reference price comes from the free Pricing API, cached for a day:

| Metric | Meaning |
| --- | --- |
| `aws_spot_cost_usd` | month-to-date spot spend |
| `aws_spot_usage_hours` | month-to-date spot instance-hours |
| `aws_spot_effective_hourly_usd` | spend / hours — what was really paid |
| `aws_spot_ondemand_hourly_usd` | on-demand list price for the same type and region |
| `aws_spot_savings_usd` | `hours * ondemand - cost` |

Spot **interruptions** are not exported here. Karpenter already publishes them
as `karpenter_interruption_received_messages_total{message_type="spot_interrupted"}`
from its SQS interruption queue.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install boto3 prometheus-client pytest
.venv/bin/python -m pytest tests/
```

The savings tests mock every boto3 client, so they need no AWS credentials.

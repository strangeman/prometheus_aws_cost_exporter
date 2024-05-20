# Prometheus Cost Exporter

## Intro

Are you looking for some system that alerts when your today's spending on cloud providers exceeds some limit?  That's just what this exporter is made for.

The exporter is a Python server that connects to AWS Cost Explorer and GCP BigQuery with a customizable period, and exposes last responses as Prometheus metrics.

## Configuration

Configuration is made through environment variables:

| Environment variable        | Description           | Default  |
| ------------- |:-------------:| -----:|
| QUERY_PERIOD      | Period to update metrics, querying AWS Cost Explorer API (0.01$ per request) | 1800 |
| AWS_ENABLED | Enable AWS metrics gathering      |   False |
| GCP_ENABLED | Enable GCP metrics gathering      |   False |
| GCP_BQ_BILLING_PROJECT | Name of GCP project with BQ billing dataset      |   False |
| GCP_BQ_DATASET_ID | Name of billing dataset in BQ      |   False |

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

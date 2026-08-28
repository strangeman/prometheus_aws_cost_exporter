# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
# Poetry >= 2.0 is required (pyproject.toml is PEP 621, not the legacy
# [tool.poetry] layout). Do not rely on a distro-packaged poetry — install it
# into its own venv: pipx install poetry
poetry install                 # creates ./.venv (Python 3.14) with dev deps

poetry run pytest tests/
poetry run pytest tests/test_savings.py::test_spot_costs_skip_groups_with_no_usage   # single test

# Run locally (needs the provider env vars below)
poetry run flask run --host 0.0.0.0    # :5000

docker build -t aws-cost-exporter .
helm install prometheus-aws-cost-exporter ./helm    # see helm/values.yaml for image + env
```

Python 3.14 only (`requires-python = ">=3.14,<4.0"`); the image is built on
`python:3.14-slim`. There is no linter or formatter configured.

## Architecture

A Flask process that polls cloud billing APIs on a timer and exposes the last
responses as Prometheus gauges. Nothing is fetched at scrape time.

[app/app.py](app/app.py) is the whole wiring: it instantiates one client class per
provider and registers each one's `fill_metrics` as an APScheduler
`IntervalTrigger` job. Metrics are served at `/metrics/` (**with** the trailing
slash — that's what `helm/values.yaml` annotates for scraping), health at `/health`.

Providers — [app/aws.py](app/aws.py) (Cost Explorer), [app/gcp.py](app/gcp.py)
(BigQuery billing export), [app/serverscom.py](app/serverscom.py) (invoice REST API),
[app/savings.py](app/savings.py) (commitment discounts + spot) — all follow the same
shape: Prometheus `Gauge`s as **class attributes**, an `<X>_ENABLED` env guard, and a
`fill_metrics()` that queries and sets. Adding a provider means adding a module in
that shape plus a `scheduler.add_job` block in `app.py`.

Consequences of gauges being class attributes: they register into the global default
registry at *import* time, so a module can only be instantiated once per process, and
metric names collide across test modules if two import the same provider. Any gauge
with labels must be `.clear()`ed before repopulating, or retired series (a deleted
Savings Plan, an instance type you stopped running) linger forever.

## Gotchas

- **`*_ENABLED` are truthy strings, not booleans.** `os.environ.get('AWS_ENABLED', default=False)`
  means `AWS_ENABLED=false` **enables** AWS. Only unset or empty disables.
- **Import-time side effects.** `app/aws.py` builds its boto3 `ce` client and
  `app/gcp.py` parses `GOOGLE_CREDENTIALS` and builds a BigQuery client in the class
  body. Importing `app.app` therefore fails outright without valid GCP credentials JSON,
  regardless of `GCP_ENABLED`. `app/savings.py` deliberately builds its clients in
  `__init__` instead — that's what makes it mockable in tests; keep new modules that way.
- **Cost Explorer bills $0.01 per request.** This drives the design: savings metrics
  run on their own `SAVINGS_QUERY_PERIOD` (default 21600s) rather than `QUERY_PERIOD`,
  and `savings.py` uses the free `Describe*`/Pricing APIs to decide whether a billed
  CE call is worth making at all (`RESERVATION_PROBES` gates
  `GetReservationUtilization` on a service actually holding an active reservation).
  Think about request count before adding a CE call or shortening a period.
- **`GetReservationUtilization` must carry a `SERVICE` filter.** Without one it returns
  an all-zero body that is indistinguishable from "no reservations exist" — not an
  account-wide total.
- **Everything in `savings.py` is month-to-date.** Those gauges climb through the month
  and reset on the 1st; they are read directly, never through `rate()`/`increase()`.
- **`pyproject.toml` lists only direct imports.** Transitive pins (botocore's
  `jmespath`/`s3transfer`, Flask's `werkzeug`/`jinja2`/`click`, …) live in
  `poetry.lock` only — do not re-add them as top-level dependencies.
- **The Dockerfile is two-stage and installs with `--no-root`.** The builder
  copies only `pyproject.toml`/`poetry.lock`, so the root package cannot be
  built there; the runtime stage gets `/app/.venv` plus `app/` and runs
  `flask` from the venv directly (no `poetry` in the final image).
- Naming split in the savings metrics: `aws_savings_plan_*` (singular) is per plan and
  always carries `savings_plan_id`; `aws_savings_plans_*` (plural) is the account total.

Env vars and the required IAM policies are documented in [README.md](README.md).

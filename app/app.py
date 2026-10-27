from flask import Flask,Response
from prometheus_client import Gauge,generate_latest
from datetime import datetime, timedelta
import os
import atexit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.aws import AWS
from app.savings import Savings
from app.gcp import GCP
from app.serverscom import Serverscom
from app.nebius import Nebius

app = Flask(__name__)

CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')
QUERY_PERIOD=os.getenv('QUERY_PERIOD', "5400")
# Cost Explorer bills per request and refreshes once a day, so the savings
# metrics run far less often than the cost ones.
SAVINGS_QUERY_PERIOD=os.getenv('SAVINGS_QUERY_PERIOD', "21600")
# Nebius APIs are free. Spot prices move every 15 minutes, so they get their own short
# period; costs come from a billing export that takes ~10s to build.
NEBIUS_PRICES_QUERY_PERIOD=os.getenv('NEBIUS_PRICES_QUERY_PERIOD', "300")
NEBIUS_COSTS_QUERY_PERIOD=os.getenv('NEBIUS_COSTS_QUERY_PERIOD', "3600")


print(f"QUERY_PERIOD: {QUERY_PERIOD}")
print(f"SAVINGS_QUERY_PERIOD: {SAVINGS_QUERY_PERIOD}")
print(f"NEBIUS_PRICES_QUERY_PERIOD: {NEBIUS_PRICES_QUERY_PERIOD}")
print(f"NEBIUS_COSTS_QUERY_PERIOD: {NEBIUS_COSTS_QUERY_PERIOD}")
print(f"AWS_ACCESS_KEY_ID: {os.environ.get('AWS_ACCESS_KEY_ID')}")

scheduler = BackgroundScheduler()

print("Job starts at: " + str(datetime.now() + timedelta(seconds=10)) )

scheduler.start()

# TODO switch to yaml config
aws_client = AWS()
scheduler.add_job(
    func=aws_client.fill_metrics,
    trigger=IntervalTrigger(seconds=int(QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=10))),
    id='aws_query',
    name='Run AWS Query',
    replace_existing=True
    )

savings_client = Savings()
scheduler.add_job(
    func=savings_client.fill_metrics,
    trigger=IntervalTrigger(seconds=int(SAVINGS_QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=20))),
    id='savings_query',
    name='Run AWS Savings Query',
    replace_existing=True
    )

gcp_client = GCP()
scheduler.add_job(
    func=gcp_client.fill_metrics,
    trigger=IntervalTrigger(seconds=int(QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=10))),
    id='gcp_query',
    name='Run GCP Query',
    replace_existing=True
    )

serverscom_client = Serverscom()
scheduler.add_job(
    func=serverscom_client.fill_metrics,
    trigger=IntervalTrigger(seconds=int(QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=10))),
    id='serverscom_query',
    name='Run Servers.com Query',
    replace_existing=True
    )

nebius_client = Nebius()
scheduler.add_job(
    func=nebius_client.fill_price_metrics,
    trigger=IntervalTrigger(seconds=int(NEBIUS_PRICES_QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=10))),
    id='nebius_prices_query',
    name='Run Nebius Prices Query',
    replace_existing=True
    )
scheduler.add_job(
    func=nebius_client.fill_cost_metrics,
    trigger=IntervalTrigger(seconds=int(NEBIUS_COSTS_QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=30))),
    id='nebius_costs_query',
    name='Run Nebius Costs Query',
    replace_existing=True
    )

# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())

@app.route('/metrics/')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.route('/health')
def health():
    return "OK"

if __name__ == '__main__':
    app.run()
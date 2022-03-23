from flask import Flask,Response
from prometheus_client import Gauge,generate_latest
import boto3
from datetime import datetime, timedelta
import time, os
import atexit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

QUERY_PERIOD = os.getenv('QUERY_PERIOD', "5400")

app = Flask(__name__)
CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')
client = boto3.client('ce')

if os.environ.get('METRIC_TODAY_DAILY_COSTS') is not None:
    g_cost = Gauge('aws_today_daily_costs', 'Today daily costs from AWS')
if os.environ.get('METRIC_YESTERDAY_DAILY_COSTS') is not None:
    g_yesterday = Gauge('aws_yesterday_daily_costs', 'Yesterday daily costs from AWS')
    g_yesterday_by_service = Gauge("aws_yesterday_costs_by_service", 'Yesterday daily costs from AWS by service', ['aws_service'])
if os.environ.get('METRIC_MONTH_TO_DATE_COSTS') is not None:
    g_mtd = Gauge('aws_month_to_date_costs', 'Month to date costs.')
if os.environ.get('METRIC_MONTH_FORCASTED_COSTS') is not None:
    g_month_forcast = Gauge('aws_month_forcasted_costs', 'Monthly forcasted cost.')

scheduler = BackgroundScheduler()

def aws_query():
    
    now = datetime.now()
    yesterday = datetime.today() - timedelta(days=1)
    tomorrow = datetime.today() + timedelta(days=1)
    two_days_ago = datetime.today() - timedelta(days=2)
    nxt_mnth = now.replace(day=28) + timedelta(days=4)
    firstday_of_nxt_mnth = nxt_mnth.strftime("%Y-%m-01")
    
    print("Calculating costs...")
    
    if os.environ.get('METRIC_TODAY_DAILY_COSTS') is not None:

        r = client.get_cost_and_usage(
            TimePeriod={
                'Start': now.strftime("%Y-%m-%d"),
                'End':  tomorrow.strftime("%Y-%m-%d")
            },
            Granularity="DAILY",
            Metrics=["BlendedCost"]
        )
        cost = r["ResultsByTime"][0]["Total"]["BlendedCost"]["Amount"]
        print("Updated AWS Daily costs: %s" %(cost))
        g_cost.set(float(cost))

    if os.environ.get('METRIC_YESTERDAY_DAILY_COSTS') is not None:
        
        r = client.get_cost_and_usage(
            TimePeriod={
                'Start': yesterday.strftime("%Y-%m-%d"),
                'End':  now.strftime("%Y-%m-%d")
            },
            Granularity="DAILY",
            Metrics=["BlendedCost"]
        )
        cost_yesterday = r["ResultsByTime"][0]["Total"]["BlendedCost"]["Amount"]
        print("Yesterday's AWS Daily costs: %s" %(cost_yesterday))
        g_yesterday.set(float(cost_yesterday))
        
        r = client.get_cost_and_usage(
            TimePeriod={
                'Start': yesterday.strftime("%Y-%m-%d"),
                'End':  now.strftime("%Y-%m-%d")
            },
            Granularity="DAILY",
            Metrics=["BlendedCost"],
            GroupBy=[
                {
                    'Type': 'DIMENSION',
                    'Key': 'SERVICE'
                }    
            ]
        )
        
        services = r["ResultsByTime"][0]["Groups"]
        # services = sorted(services, key = lambda i: i['Metrics']['BlendedCost']['Amount'])
        
        for service in services:
            service_name = service.get('Keys')[0]
            service_cost = service.get('Metrics')['BlendedCost']['Amount']
            g_yesterday_by_service.labels(service_name).set(float(service_cost))

    if os.environ.get('METRIC_MONTH_TO_DATE_COSTS') is not None:
        
        r = client.get_cost_and_usage(
            TimePeriod={
                'Start': now.strftime("%Y-%m-01"),
                'End':  firstday_of_nxt_mnth
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"]
        )
        cost_mtd = r["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"]
        print("Updated Month to date Cost: %s" %(cost_mtd))
        g_mtd.set(float(cost_mtd))
        
    if os.environ.get('METRIC_MONTH_FORCASTED_COSTS') is not None:
        
        r = client.get_cost_forecast(
            TimePeriod={
                'Start': now.strftime("%Y-%m-%d"),
                'End': firstday_of_nxt_mnth
            },
            Metric='UNBLENDED_COST',
            Granularity='MONTHLY'
            )
        cost_month_forcast = r["Total"]["Amount"]
        print("This Month's forecasted cost: %s" %(cost_month_forcast))
        g_month_forcast.set(float(cost_month_forcast))

    print("Finished calculating costs")
    return 0

@app.route('/metrics/')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.route('/health')
def health():
    return "OK"
    
# aws_query()

scheduler.start()
scheduler.add_job(
    func=aws_query,
    trigger=IntervalTrigger(seconds=int(QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=5))),
    id='aws_query',
    name='Run AWS Query',
    replace_existing=True
    )
# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())

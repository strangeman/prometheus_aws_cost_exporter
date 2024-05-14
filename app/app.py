from flask import Flask,Response
from prometheus_client import Gauge,generate_latest
from datetime import datetime, timedelta
import os
import atexit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from aws import AWS

app = Flask(__name__)

CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')
QUERY_PERIOD=os.getenv('QUERY_PERIOD', "5400")


print(f"QUERY_PERIOD: {QUERY_PERIOD}")
print(f"AWS_ACCESS_KEY_ID: {os.environ.get('AWS_ACCESS_KEY_ID')}")

scheduler = BackgroundScheduler()

print("Job starts at: " + str(datetime.now() + timedelta(seconds=10)) )

scheduler.start()

# TODO make this optional
aws_client = AWS()
scheduler.add_job(
    func=aws_client.fill_metrics,
    trigger=IntervalTrigger(seconds=int(QUERY_PERIOD),start_date=(datetime.now() + timedelta(seconds=10))),
    id='aws_query',
    name='Run AWS Query',
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
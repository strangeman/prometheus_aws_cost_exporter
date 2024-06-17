import requests
from collections import defaultdict
from datetime import datetime
from prometheus_client import Gauge
import os

SERVERSCOM_TOKEN = os.environ.get("SERVERSCOM_TOKEN")
SERVERSCOM_ENABLED = os.environ.get("SERVERSCOM_ENABLED", default=False)


class Serverscom:
    
    serverscom_monthly_expenses = Gauge("serverscom_monthly_expenses", 'Monthly expenses from servers.com', ['month_year'])
    
    def __init__(self):
        self.monthly_expenses = defaultdict(float)

    def get_serverscom_invoices(self):
        url = "https://api.servers.com/v1/billing/invoices"
        # show invoices for last 24 months
        today = datetime.today()
        start_date = (today.replace(day=1, month=today.month, year=today.year-2)).strftime("%Y-%m-%d")
        print(start_date)

        querystring = {
            "per_page": "50",
            "start_date": start_date,
        }
        headers = {
            "Authorization": f"Bearer {SERVERSCOM_TOKEN}",
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers, params=querystring)

        if response.status_code == 200:
            invoices = response.json()
            return invoices
        else:
            return None

    def get_serverscom_monthly_expenses(self):
        for invoice in self.get_serverscom_invoices():
            month_year = invoice["date"].split("-")[:2]  # e.g., ['2024', '06']
            month_year_str = "-".join(month_year)  # e.g., '2024-06'
            self.monthly_expenses[month_year_str] += invoice["total_due"]

        return self.monthly_expenses

    def fill_metrics(self):
        if not SERVERSCOM_ENABLED:
            print(f"{datetime.now()} Servers.com is not enabled.")
            return
        print(f"{datetime.now()} Calculating Servers.com costs...")
        self.serverscom_monthly_expenses.clear()
        for month_year, expense in self.get_serverscom_monthly_expenses().items():
            self.serverscom_monthly_expenses.labels(month_year).set(float(expense))
        print(f"{datetime.now()} Finished calculating Servers.com costs.")
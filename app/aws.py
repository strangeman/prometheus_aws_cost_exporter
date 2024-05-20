import boto3
from datetime import datetime, timedelta
from prometheus_client import Gauge
import os


class AWS:
    """
    The AWS class provides methods to retrieve and calculate costs from AWS using the AWS Cost Explorer API.
    It uses the boto3 library to interact with the AWS Cost Explorer API and the prometheus_client library to expose the costs as Prometheus metrics.
    """

    client = boto3.client('ce')
    AWS_ENABLED=os.environ.get('AWS_ENABLED', default=False)
    print(f"AWS_ENABLED: {AWS_ENABLED}")

    def __init__(self):
        """
        Initializes an instance of the AWS class.
        """
        pass

    def get_today_daily_costs(self):
        """
        Retrieves today's daily costs from AWS.

        Returns:
            float: The total cost for today.
        """
        r = self.client.get_cost_and_usage(
            TimePeriod={
                'Start': datetime.now().strftime("%Y-%m-%d"),
                'End':  (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            },
            Granularity="DAILY",
            Metrics=["BlendedCost"]
        )
        cost = r["ResultsByTime"][0]["Total"]["BlendedCost"]["Amount"]
        return float(cost)
    
    def get_yesterday_daily_costs(self):
        """
        Retrieves yesterday's daily costs from AWS.

        Returns:
            float: The total cost for yesterday.
        """
        r = self.client.get_cost_and_usage(
            TimePeriod={
                'Start': (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d"),
                'End':  datetime.now().strftime("%Y-%m-%d")
            },
            Granularity="DAILY",
            Metrics=["BlendedCost"]
        )
        cost = r["ResultsByTime"][0]["Total"]["BlendedCost"]["Amount"]
        return float(cost)
    
    def get_yesterday_daily_costs_by_service(self):
        """
        Retrieves yesterday's daily costs from AWS grouped by service.

        Returns:
            list: A list of dictionaries containing the service name and its cost.
        """
        r = self.client.get_cost_and_usage(
            TimePeriod={
                'Start': (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d"),
                'End':  datetime.now().strftime("%Y-%m-%d")
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
        service_costs = r["ResultsByTime"][0]["Groups"]
        service_costs = sorted(service_costs, key = lambda i: i['Metrics']['BlendedCost']['Amount'])
        return service_costs
    
    def get_month_to_date_costs(self):
        """
        Retrieves the month-to-date costs from AWS.

        Returns:
            float: The total cost for the current month.
        """
        r = self.client.get_cost_and_usage(
            TimePeriod={
                'Start': datetime.now().strftime("%Y-%m-01"),
                'End':  (datetime.now().replace(day=28) + timedelta(days=4)).strftime("%Y-%m-01")
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"]
        )
        cost = r["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"]
        return float(cost)
    
    def get_month_forecasted_costs(self):
        """
        Retrieves the forecasted costs for the current month from AWS.

        Returns:
            float: The forecasted cost for the current month.
        """
        r = self.client.get_cost_forecast(
            TimePeriod={
                'Start': datetime.now().strftime("%Y-%m-%d"),
                'End':  (datetime.now().replace(day=28) + timedelta(days=4)).strftime("%Y-%m-01")
            },
            Metric='UNBLENDED_COST',
            Granularity='MONTHLY'
            )
        cost = r["Total"]["Amount"]
        return float(cost)

    def fill_metrics(self):
        """
        Fills the Prometheus metrics with the AWS costs.

        This method retrieves the costs from AWS using the methods defined above and sets the corresponding Prometheus metrics.
        """
        if not self.AWS_ENABLED:
            print(f"{datetime.now()} AWS is not enabled.")
            return

        print(f"{datetime.now()} Calculating AWS costs...")

        aws_today_daily_costs = Gauge('aws_today_daily_costs', 'Today daily costs from AWS')
        aws_today_daily_costs.set(self.get_today_daily_costs())

        aws_yesterday_daily_costs = Gauge('aws_yesterday_daily_costs', 'Yesterday daily costs from AWS')
        aws_yesterday_daily_costs.set(self.get_yesterday_daily_costs())
        aws_yesterday_costs_by_service = Gauge("aws_yesterday_costs_by_service", 'Yesterday daily costs from AWS by service', ['aws_service'])
        aws_yesterday_costs_by_service.clear()
        for service in self.get_yesterday_daily_costs_by_service():
            service_name = service.get('Keys')[0]
            service_cost = service.get('Metrics')['BlendedCost']['Amount']
            aws_yesterday_costs_by_service.labels(service_name).set(float(service_cost))

        aws_month_to_date_costs = Gauge('aws_month_to_date_costs', 'Month to date costs.')
        aws_month_to_date_costs.set(self.get_month_to_date_costs())

        aws_month_forecasted_costs = Gauge('aws_month_forecasted_costs', 'Monthly forecasted cost.')
        aws_month_forecasted_costs.set(self.get_month_forecasted_costs())

        print(f"{datetime.now()} Finished calculating AWS costs...")

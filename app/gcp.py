from google.cloud import bigquery
from datetime import datetime, timedelta
from prometheus_client import Gauge
import os
import json
from google.oauth2 import service_account

class GCP:
    
    json_account_info = json.loads(os.environ.get('GOOGLE_CREDENTIALS'))  # convert JSON to dictionary
    credentials = service_account.Credentials.from_service_account_info(json_account_info)
    # Configure BigQuery client
    client = bigquery.Client(credentials=credentials)
    
    # Set project ID and dataset where billing data resides
    GCP_BQ_BILLING_PROJECT = os.environ.get('GCP_BQ_BILLING_PROJECT')
    GCP_BQ_DATASET_ID = os.environ.get('GCP_BQ_DATASET_ID')
    GCP_ENABLED=os.environ.get('GCP_ENABLED', default=False)

    gcp_yesterday_costs_by_service = Gauge("gcp_yesterday_costs_by_service", 'Yesterday daily costs from gcp by service', ['gcp_service', 'gcp_project'])


    def __init__(self):
        """
        Initializes an instance of the GCP class.
        """
        pass
    
    def get_costs_by_day(self, date):
        next_date = date + timedelta(days=1)

        query = f"""
        SELECT
          service.description AS `service_description`,
          project.name AS `project_name`,
          SUM(CAST(cost AS NUMERIC)) AS `cost`,
          SUM(IFNULL((
              SELECT
                SUM(CAST(c.amount AS numeric))
              FROM
                UNNEST(credits) c
              WHERE
                c.type IN ('')), 0)) AS `discounts`,
          SUM(IFNULL((
              SELECT
                SUM(CAST(c.amount AS numeric))
              FROM
                UNNEST(credits) c
              WHERE
                c.type IN ('')), 0)) AS `promotions`,
          SUM(CAST(cost AS NUMERIC)) + SUM(IFNULL((
              SELECT
                SUM(CAST(c.amount AS numeric))
              FROM
                UNNEST(credits) c
              WHERE
                c.type IN ('')), 0)) + SUM(IFNULL((
              SELECT
                SUM(CAST(c.amount AS numeric))
              FROM
                UNNEST(credits) c
              WHERE
                c.type IN ('')), 0)) AS `subtotal`
        FROM
          `{self.GCP_BQ_BILLING_PROJECT}.{self.GCP_BQ_DATASET_ID}.*`
        WHERE
          DATE(usage_start_time) >= DATE('{date.strftime("%Y-%m-%d")}')
          AND DATE(usage_start_time) < DATE('{next_date.strftime("%Y-%m-%d")}')
        GROUP BY
          service.description, project.name
        ORDER BY
          Subtotal DESC;
        """
        # Execute the query and get results
        query_job = self.client.query(query)
        results = query_job.result()
        return results
    
    def fill_metrics(self):
        if not self.GCP_ENABLED:
            print(f"{datetime.now()} GCP is not enabled.")
            return
        print(f"{datetime.now()} Calculating GCP costs...")
        self.gcp_yesterday_costs_by_service.clear()
        for service in self.get_costs_by_day(datetime.today()-timedelta(days=1)):
            service_name = service.service_description
            project = service.project_name
            service_cost = service.subtotal
            self.gcp_yesterday_costs_by_service.labels(service_name, project).set(float(service_cost))
        print(f"{datetime.now()} Finished calculating GCP costs...")
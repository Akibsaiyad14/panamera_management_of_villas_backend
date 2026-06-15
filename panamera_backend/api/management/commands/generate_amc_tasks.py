import datetime
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from psycopg2.extras import execute_values
from dateutil.relativedelta import relativedelta
from api.utils import execute_query, generate_jobs_and_tasks_for_amc

# How many days into the future should we generate jobs? 14-30 is a good range.
class Command(BaseCommand):
    help = 'Generates AMC visit jobs and their tasks in a rolling window.'

    def handle(self, *args, **options):
        self.stdout.write("Starting nightly AMC job and task generation...")
        
        today = datetime.date.today()
        # We generate for the next 30 days to ensure there's always a buffer.
        end_date = today + datetime.timedelta(days=30)
        
        # Get all active, non-deleted AMCs
        active_amcs = execute_query('SELECT "amcId", "villaId", "amcJobName" FROM "AMCMaster" WHERE "isDeleted" = 0 AND status = 0', many=True)

        total_jobs = 0
        total_tasks = 0
        
        with transaction.atomic():
            for amc in active_amcs:
                amc_id = amc['amcId']
                villa_id = amc.get('villaId')
                amc_job_name = amc.get('amcJobName')
                # The helper function does all the heavy lifting
                job_count, task_count = generate_jobs_and_tasks_for_amc(amc_id, today, end_date)
                total_jobs += job_count
                total_tasks += task_count
                # Log details for each AMC processed
                self.stdout.write(
                    f"Processed AMC ID: {amc_id}, Job Name: {amc_job_name}, Villa ID: {villa_id or 'None'}, "
                    f"Generated/Verified {job_count} jobs, {task_count} tasks."
                )

        self.stdout.write(self.style.SUCCESS(
            f"Nightly run complete. Generated/verified {total_jobs} jobs and {total_tasks} tasks across all AMCs."
        ))

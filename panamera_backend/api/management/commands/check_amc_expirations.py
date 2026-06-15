import os
import datetime
import firebase_admin
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from firebase_admin import credentials
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from api.utils import _send_notification, execute_query
from api.constants import *


class Command(BaseCommand):
    help = 'Checks for AMCs that are expiring soon, expiring today, or have just expired, and notifies the relevant supervisor and customer.'

    def _send_email(self, email_config, recipient_email, subject, body):
        try:
            msg = MIMEMultipart()
            msg['From'] = email_config['emailId']
            msg['To'] = recipient_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            smtp_port = int(email_config['smtpPort'])
            if smtp_port == 465:
                server = smtplib.SMTP_SSL(email_config['smtpServer'], smtp_port)
            else:
                server = smtplib.SMTP(email_config['smtpServer'], smtp_port)
                server.starttls()
            server.login(email_config['emailId'], email_config['appPassword'])
            text = msg.as_string()
            server.sendmail(email_config['emailId'], recipient_email, text)
            server.quit()
            self.stdout.write(f"    - Successfully sent email to {recipient_email}")
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"    - FAILED to send email to {recipient_email}: {e}"))

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(f"[{timezone.now()}] Running AMC Expiration Scan..."))

        # --- Initialize Firebase Admin SDK ---
        if not firebase_admin._apps:
            # ... (your existing Firebase init code remains here) ...
            service_account_path = os.path.join(settings.BASE_DIR, 'firebase-service-account.json')
            if not os.path.exists(service_account_path):
                self.stderr.write(self.style.ERROR(f"FATAL: Firebase key not found at: {service_account_path}"))
                return
            try:
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"FATAL: Failed to initialize Firebase: {e}"))
                return

        # --- Fetch and Prepare Email Configuration ---
        email_config = None
        try:
            email_query = 'SELECT "emailId", "appPassword", "smtpServer", "smtpPort" FROM "emailSettings" WHERE COALESCE("isDeleted", 0) = 0 LIMIT 1'
            
            # FIXED: Handle execute_query returning a list with one dictionary, or an empty list.
            settings_list = execute_query(email_query) # `many=False` is default
            
            if settings_list:
                # If the list is not empty, get the first (and only) dictionary from it.
                email_config = settings_list[0]
            else:
                self.stdout.write(self.style.WARNING("Email settings not found in database. Email notifications will be skipped."))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Could not fetch email settings: {e}. Email notifications will be skipped."))

        # --- Main Logic ---
        try:
            today = datetime.date.today()
            
            query = """
                SELECT
                    m."amcId", m."jobId", m."amcJobName", m."startDate", m.duration,
                    (m."startDate" + (m.duration * INTERVAL '1 month') - INTERVAL '1 day')::date AS end_date,
                    m."gardenSupervisorId",
                    m."poolSupervisorId",
                    u.id AS "supervisor_int_id",
                    u."fcmToken" AS "supervisor_fcm_token",
                    m."customerId" AS "customer_string_id",
                    c."customerName",
                    c.email AS "customer_email",
                    c."fcmToken" AS "customer_fcm_token"
                FROM "AMCMaster" m
                LEFT JOIN "user" u ON m."supervisorId" = u."employeeId"
                LEFT JOIN customer c ON m."customerId" = c."customerId"
                WHERE m.status = %s AND COALESCE(m."isDeleted", 0) = 0
                  AND (m."startDate" + (m.duration * INTERVAL '1 month') - INTERVAL '1 day')::date
                      IN (%s, %s, %s)
            """
            params = [
                AMC_STATUS_ACTIVE,
                today + datetime.timedelta(days=10),
                today,
                today - datetime.timedelta(days=1)
            ]
            amcs_to_notify = execute_query(query, params, many=True)

            if not amcs_to_notify:
                self.stdout.write(self.style.SUCCESS('Scan complete. No AMCs found requiring expiration alerts.'))
                return

            self.stdout.write(self.style.WARNING(f"Found {len(amcs_to_notify)} AMC(s) requiring expiration alerts."))
            
            amcs_to_mark_completed = []
            for amc in amcs_to_notify:
                end_date = amc['end_date']
                days_diff = (end_date - today).days
                customer_name = amc.get('customerName') or 'Valued Customer'
                amc_job_name = amc['amcJobName']
                job_id = amc['jobId']

                # ... (Notification content logic is fine, no changes needed here) ...
                if days_diff == 10:
                    sup_title, sup_body = "AMC Expiring Soon", f"The AMC '{amc_job_name}' for customer '{customer_name}' (Job ID: {job_id}) will expire in 10 days."
                    cust_title, cust_body = "Your AMC Contract is Expiring Soon", f"Dear {customer_name}, your AMC contract '{amc_job_name}' will expire in 10 days. Please contact us to renew."
                elif days_diff == 0:
                    sup_title, sup_body = "AMC Expires Today", f"The AMC '{amc_job_name}' for customer '{customer_name}' (Job ID: {job_id}) expires today."
                    cust_title, cust_body = "Your AMC Contract Expires Today", f"Dear {customer_name}, your AMC contract '{amc_job_name}' expires today. Please contact us for renewal."
                elif days_diff < 0:
                    sup_title, sup_body = "AMC Expired", f"The AMC '{amc_job_name}' for customer '{customer_name}' (Job ID: {job_id}) expired and is being auto-completed."
                    cust_title, cust_body = "Your AMC Contract Has Expired", f"Dear {customer_name}, your AMC contract '{amc_job_name}' has expired. We hope you were satisfied with our service."
                    amcs_to_mark_completed.append(amc['amcId'])
                else:
                    continue

                # --- 1. Notify Supervisor ---
                supervisor_int_id, supervisor_fcm_token = amc.get('supervisor_int_id'), amc.get('supervisor_fcm_token')
                if supervisor_int_id:
                    self.stdout.write(f"  - Sending alert for AMC ID {amc['amcId']} to SUPERVISOR {supervisor_int_id}.")
                    _send_notification(
                        recipient_user_id=supervisor_int_id,
                        title=sup_title, body=sup_body, notification_type="AMC_EXPIRATION_ALERT",
                        data_payload={"amcId": str(amc['amcId']), "jobId": job_id, "type": "AMC_EXPIRATION_ALERT"},
                        fcm_token=supervisor_fcm_token, delay_seconds=0
                    )
                else:
                    self.stdout.write(f"  - SKIPPING supervisor notification for AMC ID {amc['amcId']} (no matching user found for supervisorId '{amc['supervisorId']}').")

                # --- 2. Notify Customer (Push Notification) ---
                customer_string_id, customer_fcm_token = amc.get('customer_string_id'), amc.get('customer_fcm_token')
                if customer_string_id:
                    self.stdout.write(f"  - Sending alert for AMC ID {amc['amcId']} to CUSTOMER {customer_string_id}.")
                    _send_notification(
                        recipient_customer_id=customer_string_id,
                        title=cust_title, body=cust_body, notification_type="AMC_EXPIRATION_ALERT",
                        data_payload={"amcId": str(amc['amcId']), "jobId": job_id, "type": "AMC_EXPIRATION_ALERT"},
                        fcm_token=customer_fcm_token, delay_seconds=0
                    )
                else:
                    self.stdout.write(f"  - SKIPPING customer push notification for AMC ID {amc['amcId']} (missing customerId link in AMCMaster).")
                
                # --- 3. Notify Customer (Email) ---
                customer_email = amc.get('customer_email')
                if email_config and customer_email:
                    self.stdout.write(f"  - Sending email for AMC ID {amc['amcId']} to CUSTOMER ({customer_email}).")
                    self._send_email(email_config, customer_email, cust_title, cust_body)
                else:
                    self.stdout.write(f"  - SKIPPING customer email for AMC ID {amc['amcId']} (missing email address or SMTP config).")

            # --- Atomically Update Status for Expired AMCs ---
            if amcs_to_mark_completed:
                with transaction.atomic():
                    self.stdout.write(f"Marking {len(amcs_to_mark_completed)} expired AMC(s) as 'Completed'...")
                    update_query = 'UPDATE "AMCMaster" SET status = %s WHERE "amcId" = ANY(%s)'
                    execute_query(update_query, [AMC_STATUS_COMPLETED, amcs_to_mark_completed]) # Should use fetch=False here
                    self.stdout.write(self.style.SUCCESS("Batch status update complete."))

            self.stdout.write(self.style.SUCCESS("AMC Expiration Scan finished successfully."))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"An unexpected error occurred during the AMC scan: {e}"))

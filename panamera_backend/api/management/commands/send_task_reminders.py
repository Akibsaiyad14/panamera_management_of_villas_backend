import os
import firebase_admin
from firebase_admin import credentials
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from api.utils import _send_notification, execute_query, log_activity_raw  # Adjust the import path as necessary
import traceback
from datetime import date
from api.constants import ISSUE, TASK


class Command(BaseCommand):
    help = 'Sends recurring reminders for active tasks and issues based on their reminder settings (Daily, Weekly Once, Weekly Twice).'

    def _generate_notification_body(self, item):
        """Generates a dynamic notification body for recurring reminders for both Tasks and Issues."""
        item_name = item.get('taskName', 'Unnamed Item')
        due_date_str = f" and is due on {item['dueDate'].strftime('%d-%b-%Y')}" if item.get('dueDate') else ""
        reminder_type = item.get('reminder', 'Task')

        # Determine if the item is a Task or an Issue
        item_type_str = "Issue" if item.get('taskType') == ISSUE else "Task"

        if reminder_type == 'Daily':
            return f"Daily Reminder: The {item_type_str.lower()} '{item_name}' is active{due_date_str}."
        else: # Covers Weekly Once/Twice
            return f"Weekly Reminder: The {item_type_str.lower()} '{item_name}' is active{due_date_str}."

    def handle(self, *args, **options):
        self.stdout.write(f"[{timezone.now()}] Running Recurring Task & Issue Reminder Scan (Timezone: Asia/Dubai)...")

        # --- Initialize Firebase Admin SDK ---
        if not firebase_admin._apps:
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

        try:
            # The query already fetches taskType, so no changes are needed here.
            potential_items_query = """
                SELECT
                    t.id AS item_id,
                    t."taskName",
                    t.reminder,
                    t."dueDate",
                    t."startDate",
                    t."supervisorId",
                    t."taskType",
                    su."fcmToken", -- Still select the token, it will be NULL if not present
                    su.id AS supervisor_user_id,
                    su."employeeId" AS supervisor_employee_id,
                    su."fullName" AS "supervisorName"
                FROM "taskManager" t
                JOIN "user" su ON t."supervisorId" = su."employeeId"
                WHERE
                    COALESCE(t."isDeleted", 0) = 0
                    AND t."taskStatus" = 0
                    AND t.reminder IS NOT NULL AND t.reminder != 'None'
                    AND t."startDate" IS NOT NULL AND t."dueDate" IS NOT NULL
                    AND (CURRENT_DATE AT TIME ZONE 'Asia/Dubai') >= t."startDate"
                    AND (CURRENT_DATE AT TIME ZONE 'Asia/Dubai') <= t."dueDate";
            """
            
            potential_items = execute_query(potential_items_query, many=True)

            if not potential_items:
                self.stdout.write(self.style.SUCCESS('Scan complete. No open tasks or issues require reminders today.'))
                log_activity_raw(
                    request=None, category='ReminderCron', action='ExecutionSuccess', performer=None,
                    details={'found_items': 0, 'sent_notifications': 0}
                )
                return

            self.stdout.write(f"Found {len(potential_items)} potentially active task(s)/issue(s). Filtering for today's reminders...")
            
            items_to_notify = []
            today = timezone.now().astimezone(timezone.get_fixed_timezone(240)).date()

            for item in potential_items:
                should_send = False
                reminder_type = item.get('reminder')
                start_date = item.get('startDate')

                if not start_date:
                    continue

                if reminder_type == 'Daily':
                    should_send = True
                
                elif reminder_type == 'Weekly Once':
                    if today.weekday() == start_date.weekday():
                        should_send = True
                
                elif reminder_type == 'Weekly Twice':
                    first_reminder_day = start_date.weekday()
                    second_reminder_day = (first_reminder_day + 3) % 7 
                    
                    if today.weekday() in [first_reminder_day, second_reminder_day]:
                        should_send = True

                if should_send:
                    items_to_notify.append(item)
            
            if not items_to_notify:
                self.stdout.write(self.style.SUCCESS('Filtering complete. No reminders are scheduled for today.'))
                log_activity_raw(
                    request=None, category='ReminderCron', action='ExecutionSuccess', performer=None,
                    details={'found_items': len(potential_items), 'sent_notifications': 0}
                )
                return

            self.stdout.write(self.style.SUCCESS(f"Found {len(items_to_notify)} task(s)/issue(s) to notify."))
            
            for item in items_to_notify:
                fcm_token = item['fcmToken']
                item_id = item['item_id']
                item_type = item.get('taskType', TASK) # Default to TASK if null
                supervisor_user_id = item['supervisor_user_id']
                supervisor_name = item.get('supervisorName', 'N/A')
                
                # --- Dynamically set notification content based on item type ---
                if item_type == ISSUE:
                    title = "Issue Reminder"
                    notification_type = "ISSUE_REMINDER"
                    item_type_str = "Issue"
                else: # Default to Task
                    title = "Task Reminder"
                    notification_type = "TASK_REMINDER"
                    item_type_str = "Task"
                
                body = self._generate_notification_body(item)
                
                self.stdout.write(f"  - Sending {item_type_str} reminder for ID: {item_id} to Supervisor: {supervisor_name}")

                data_payload = {
                    'notificationType': notification_type.lower(),
                    'taskId': str(item_id), # Keep key as 'taskId' for client-side consistency
                    'taskType': str(item_type),
                }

                _send_notification(
                    recipient_user_id=supervisor_user_id,
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    data_payload=data_payload,
                    fcm_token=fcm_token
                )
                
                # Log each notification sent
                log_activity_raw(
                    request=None,
                    category='ReminderCron',
                    action='NotificationSent',
                    performer=None,
                    target_employee_name=supervisor_name,
                    details={
                        'supervisorName': supervisor_name,
                        'item_name': item.get('taskName'),
                        'item_type': item_type_str,
                        'item_id': item_id
                    }
                )
            
            self.stdout.write(self.style.SUCCESS(f"Successfully sent {len(items_to_notify)} reminder(s)."))

            # Log overall success
            log_activity_raw(
                request=None,
                category='ReminderCron',
                action='ExecutionSuccess',
                performer=None,
                details={
                    'found_items': len(potential_items),
                    'sent_notifications': len(items_to_notify)
                }
            )

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"An unexpected error occurred during the reminder scan: {e}"))
            traceback.print_exc()

            # Log the error
            log_activity_raw(
                request=None,
                category='ReminderCron',
                action='ExecutionError',
                performer=None,
                details={
                    'error': str(e),
                    'traceback': traceback.format_exc()
                }
            )

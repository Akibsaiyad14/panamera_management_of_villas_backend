import os
import firebase_admin
from firebase_admin import credentials
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from api.utils import _send_notification, execute_query
from django.db import transaction
from api.constants import STATUS_PENDING


class Command(BaseCommand):
    help = 'Escalates unread AND pending manager alerts to Superadmins, linking the new alert to the attendance record.'

    def handle(self, *args, **options):
        self.stdout.write(f"[{timezone.now()}] Running PENDING Manager Alert Escalation Scan (Timezone: Asia/Dubai)...")

        if not firebase_admin._apps:
            service_account_path = os.path.join(settings.BASE_DIR, 'firebase-service-account.json')
            if not os.path.exists(service_account_path):
                self.stderr.write(self.style.ERROR(f"FATAL: Firebase key not found at: {service_account_path}"))
                return
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)

        with transaction.atomic():
            try:
                # =================================================================
                # STEP 1: Fetch details including supervisor Role ID
                # =================================================================
                stale_pending_alerts_query = """
                    SELECT
                        n."notificationId" as original_notification_id,
                        supervisor."fullName" as supervisor_name,
                        supervisor."roleId" as supervisor_role_id,   -- ADDED: Fetch Role ID
                        a.date as attendance_date,
                        employee."fullName" as employee_name,
                        a.id as attendance_id
                    FROM "notifications" n
                    JOIN "attendance" a ON n."attendanceId" = a.id
                    JOIN "user" as employee ON a."labourUserId" = employee.id
                    JOIN "user" as supervisor ON supervisor.id = employee."reportingToId"
                    WHERE
                        n."attendanceId" IS NOT NULL
                        AND n.type IN ('EMPLOYEE_OVERTIME_ALERT', 'EARLY_LEAVE_ALERT')
                        AND n."isRead" = FALSE
                        AND n."escalationSent" = FALSE
                        AND employee."reportingToId" IS NOT NULL
                        AND supervisor."isDeleted" = 0
                        AND employee."isDeleted" = 0
                        AND n."createdAt" <= (NOW() AT TIME ZONE 'Asia/Dubai' - INTERVAL '2 days')
                        AND n."userId" = supervisor.id
                        AND (
                            (n.type = 'EMPLOYEE_OVERTIME_ALERT' AND a."overtimeStatus" = %s)
                            OR
                            (n.type = 'EARLY_LEAVE_ALERT' AND a."earlyReasonStatus" = %s)
                        )
                """
                stale_alerts = execute_query(stale_pending_alerts_query, [STATUS_PENDING, STATUS_PENDING], many=True)

                if not stale_alerts:
                    self.stdout.write(self.style.SUCCESS('Scan complete. No pending and unread manager alerts found.'))
                    return

                self.stdout.write(f"Found {len(stale_alerts)} PENDING and unread alert(s) to escalate to admins.")
                
                # --- STEP 2: Get Superadmins ---
                admin_query = "SELECT id, \"fcmToken\" FROM \"user\" WHERE \"roleId\" IN (1, 2, 3) AND \"isDeleted\" = 0"
                all_superadmins = execute_query(admin_query, many=True)

                if not all_superadmins:
                    self.stderr.write(self.style.WARNING("CRITICAL: Found alerts to escalate, but no superadmin accounts exist."))
                    return

                self.stdout.write(f"Escalations will be sent to {len(all_superadmins)} superadmin account(s).")
                
                escalated_ids_to_mark = []
                
                # Role IDs for Admins (Main Admin, Office Admin, Superadmin)
                ADMIN_ROLES = [1, 2, 3]

                for alert in stale_alerts:
                    # --- Extract data ---
                    original_id = alert['original_notification_id']
                    supervisor_name = alert['supervisor_name']
                    supervisor_role_id = alert['supervisor_role_id']
                    employee_name = alert['employee_name']  # Employee whose request is pending
                    attendance_date = alert['attendance_date'].strftime('%Y-%m-%d')
                    attendance_id = alert['attendance_id']

                    self.stdout.write(f"  - Escalating alert for '{supervisor_name}' (Role: {supervisor_role_id}) - Employee: {employee_name}.")
                    
                    # --- Prepare the notification content based on Role ---
                    title = "Escalation: Unread alert"
                    
                    if supervisor_role_id in ADMIN_ROLES:
                        # Message for Admins (Role 1, 2, 3) including their NAME and employee name
                        body = f"Super admin or officeadmin {supervisor_name} has not approved/rejected {employee_name}'s overtime/early leave on the date {attendance_date}."
                    else:
                        # Message for regular Supervisors including employee name
                        body = f"Supervisor {supervisor_name} has not approved/rejected {employee_name}'s overtime/early leave on the date {attendance_date}."

                    notification_type = "MANAGER_ESCALATION_ALERT"
                    data_payload = {
                        'notificationType': 'unread_supervisor_alert',
                        'originalNotificationId': str(original_id),
                        'unresponsiveSupervisorName': supervisor_name,
                        'employeeName': employee_name,  # Added employee name to payload
                        'attendanceDate': attendance_date
                    }

                    # --- Send the notification to each admin ---
                    for admin in all_superadmins:
                        _send_notification(
                            recipient_user_id=admin['id'],
                            title=title, body=body,
                            notification_type=notification_type,
                            data_payload=data_payload.copy(),
                            fcm_token=admin.get('fcmToken'),
                            delay_seconds=0,
                            attendance_id=attendance_id
                        )

                    escalated_ids_to_mark.append(original_id)

                # --- Mark original alerts as escalated ---
                if escalated_ids_to_mark:
                    unique_ids = list(set(escalated_ids_to_mark))
                    self.stdout.write(f"Marking {len(unique_ids)} original supervisor alerts as escalated...")
                    update_query = 'UPDATE "notifications" SET "escalationSent" = TRUE WHERE "notificationId" = ANY(%s)'
                    execute_query(update_query, [unique_ids])
                    self.stdout.write(self.style.SUCCESS("Batch update complete. Escalation finished."))

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"An unexpected error occurred during the escalation scan: {e}"))

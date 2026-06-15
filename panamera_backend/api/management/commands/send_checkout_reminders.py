import os
import json
import firebase_admin
from firebase_admin import credentials, messaging
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from api.utils import execute_query  # Make sure to adjust the import path to your project structure


class Command(BaseCommand):
    help = 'Scans for Dubai users who missed checkout and sends a reminder.'

    def handle(self, *args, **options):
        # 1. Use Dubai Timezone for logging
        import pytz
        dubai_tz = pytz.timezone('Asia/Dubai')
        now_dubai = timezone.now().astimezone(dubai_tz)
        
        self.stdout.write(f"[{now_dubai}] Running checkout reminder scan (Dubai Time)...")

        # --- Initialize Firebase Admin SDK ---
        if not firebase_admin._apps:
            service_account_path = os.path.join(settings.BASE_DIR, 'firebase-service-account.json')
            if not os.path.exists(service_account_path):
                self.stderr.write(self.style.ERROR("FATAL: Firebase service account key not found."))
                return
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)

        # --- Optimized Query ---
        # 1. We ensure checkInTime is NOT NULL (user actually started work).
        # 2. We use 'Asia/Dubai' for comparison.
        # 3. We handle shifts that cross midnight (if endTime < startTime, it ends the next day).
        users_to_notify_query = """
            SELECT
                u.id AS user_id, 
                u."fcmToken", 
                u."fullName", 
                a.id AS attendance_id, 
                s."endTime",
                s."startTime"
            FROM "attendance" a
            JOIN "shifts" s ON a."assignedShiftAtCheckInId" = s."shiftId"
            JOIN "user" u ON a."labourUserId" = u.id
            WHERE
                a."isDeleted" = 0 
                AND a."checkInTime" IS NOT NULL            -- MUST have checked in
                AND a."checkOutTime" IS NULL               -- MUST NOT have checked out
                AND a."checkoutReminderSent" = FALSE       -- Only send once
                AND u."fcmToken" IS NOT NULL               -- Must have a device token
                AND (
                    CASE 
                        -- If endTime is less than startTime, it's a night shift ending the next day
                        WHEN s."endTime" < s."startTime" THEN (a.date + s."endTime" + INTERVAL '1 day')
                        ELSE (a.date + s."endTime")
                    END
                    + INTERVAL '15 minutes'                -- Grace period
                ) <= NOW() AT TIME ZONE 'Asia/Dubai'       -- Compare against Dubai Time
        """

        try:
            users = execute_query(users_to_notify_query)

            if not users:
                self.stdout.write(self.style.SUCCESS('Scan complete. No users to notify.'))
                return

            self.stdout.write(f"Found {len(users)} user(s) to notify.")
            
            reminded_attendance_ids = []
            for user in users:
                checkout_time_str = user['endTime'].strftime("%I:%M %p")
                title = "Checkout Reminder"
                body = f"Hi {user['fullName']}, your shift ended at {checkout_time_str}. Please remember to check out from the app."
                
                try:
                    # STEP 1: Insert Notification
                    insert_query = """
                        INSERT INTO "notifications" ("userId", "title", "body", "type", "createdAt")
                        VALUES (%s, %s, %s, 'CHECKOUT_REMINDER', NOW())
                        RETURNING "notificationId"
                    """
                    insert_result = execute_query(insert_query, [user['user_id'], title, body], fetch=True)
                    notification_id = insert_result[0]['notificationId']

                    # STEP 2: Prepare Payload
                    data_payload = {
                        'notificationType': 'check_out_reminder',
                        'notificationId': str(notification_id),
                        'attendanceId': str(user['attendance_id'])
                    }
                    
                    # Update payload in DB
                    execute_query("""
                        UPDATE "notifications" SET "dataPayload" = %s::jsonb WHERE "notificationId" = %s
                    """, [json.dumps(data_payload), notification_id])

                    # STEP 3: Send Push
                    message = messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        data=data_payload,
                        token=user['fcmToken'],
                        apns=messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound='default')))
                    )
                    messaging.send(message)
                    
                    reminded_attendance_ids.append(user['attendance_id'])
                    self.stdout.write(f"  - Notified: {user['fullName']} (ID: {user['user_id']})")

                except Exception as e:
                    self.stderr.write(f"Error processing user {user['user_id']}: {e}")

            # STEP 4: Batch Update Attendance
            if reminded_attendance_ids:
                execute_query('UPDATE "attendance" SET "checkoutReminderSent" = TRUE WHERE id = ANY(%s)', [reminded_attendance_ids])
                self.stdout.write(self.style.SUCCESS(f"Successfully processed {len(reminded_attendance_ids)} notifications."))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Main Loop Error: {e}"))

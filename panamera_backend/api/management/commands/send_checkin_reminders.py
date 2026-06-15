from django.core.management.base import BaseCommand
from django.db import connection
from datetime import datetime, timedelta, time
from django.utils import timezone
from django.conf import settings
import pytz
import time as time_module
from api.utils import _send_notification

class Command(BaseCommand):
    help = "Sends check-in reminders to employees who haven't checked in 30 minutes after their shift starts."

    def handle(self, *args, **options):
        # Get the timezone from settings (Asia/Dubai)
        tz = pytz.timezone(settings.TIME_ZONE)
        
        # Get current time in the configured timezone
        now = datetime.now(tz)
        current_date = now.date()
        current_time = now.time()
        
        # Skip on Sundays (weekday 6)
        if now.weekday() == 6:
            self.stdout.write(self.style.WARNING(f"Skipping check-in reminders on Sunday. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"))
            return
        
        self.stdout.write(f"Starting check-in reminder process at: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        try:
            with connection.cursor() as cursor:
                # Find employees who:
                # 1. Are Active and not deleted
                # 2. Have a shift assigned
                # 3. Their shift started more than 30 minutes ago
                # 4. Do NOT have an attendance record for today
                # 5. Have permission for mobile attendance
                
                find_late_checkins_query = """
                    SELECT 
                        u.id as user_id,
                        u."fullName",
                        u."employeeId",
                        u."fcmToken",
                        s."shiftId",
                        s."startTime",
                        s."endTime",
                        s."shiftName"
                    FROM public."user" u
                    JOIN public.shifts s ON u."shiftId" = s."shiftId"
                    JOIN public.userrole ur ON u."roleId" = ur."roleId"
                    LEFT JOIN public.attendance a ON u.id = a."labourUserId" AND a."date" = %s
                    WHERE
                        u."employmentStatus" = 'Active'
                        AND u."isDeleted" = 0
                        AND s."isDeleted" = 0
                        AND u."shiftId" IS NOT NULL
                        AND a.id IS NULL
                        -- Check if role has mobile attendance permission
                        AND ur."functionalityKey"::jsonb @> '"mobile_attendance_my_attendance"'::jsonb
                        -- Check if shift started more than 30 minutes ago
                        AND (
                            CASE
                                -- For shifts that start before midnight and we're checking on the same day
                                WHEN s."startTime" <= %s THEN
                                    s."startTime" + interval '30 minutes' <= %s
                                -- For night shifts that started yesterday (crosses midnight)
                                WHEN s."startTime" > %s THEN
                                    -- Only alert if we're past midnight and it's been 30 min since start on previous day
                                    (s."startTime" + interval '30 minutes' - interval '24 hours') <= %s
                                ELSE FALSE
                            END
                        )
                        -- Check if shift has not ended yet
                        AND (
                            CASE
                                -- For regular shifts where end time is after start time (same day shift)
                                WHEN s."endTime" > s."startTime" THEN
                                    %s < s."endTime"
                                -- For night shifts where end time is before start time (crosses midnight)
                                WHEN s."endTime" < s."startTime" THEN
                                    -- Either we're still in the day of shift start (before midnight)
                                    -- OR we're after midnight but before end time
                                    (%s >= s."startTime" OR %s < s."endTime")
                                ELSE TRUE
                            END
                        );
                """
                
                # Parameters for the query
                params = [
                    current_date,      # For attendance join
                    current_time,      # Check if shift start is before current time
                    current_time,      # Check if 30 min has passed
                    current_time,      # For night shift comparison
                    current_time,      # For night shift 30 min check
                    current_time,      # Check if shift has not ended (regular shift)
                    current_time,      # Check if still in shift day (night shift)
                    current_time       # Check if before end time (night shift)
                ]

                cursor.execute(find_late_checkins_query, params)
                late_employees = cursor.fetchall()

                if not late_employees:
                    self.stdout.write(self.style.SUCCESS("No employees need check-in reminders. Process complete."))
                    return

                self.stdout.write(f"Found {len(late_employees)} employees who need check-in reminders.")

                # Send notifications to each employee with delay to avoid rate limiting
                notifications_sent = 0
                failed_notifications = 0
                
                for index, (user_id, full_name, employee_id, fcm_token, shift_id, start_time, end_time, shift_name) in enumerate(late_employees, 1):
                    try:
                        # Skip if no FCM token
                        if not fcm_token:
                            self.stdout.write(f"  ⊘ Skipping {full_name} (ID: {employee_id}) - No FCM token")
                            continue
                        
                        # Prepare notification data
                        title = "Check-In Reminder ⏰"
                        body = f"Hi {full_name}, your shift started at {start_time}. Please check in as soon as possible."
                        notification_type = "check_in_reminder"
                        data_payload = {
                            'notificationType': 'check_in_reminder',
                            'shiftId': str(shift_id),
                            'shiftName': shift_name,
                            'startTime': str(start_time)
                        }
                        
                        # Use the utility function to send notification
                        _send_notification(
                            title=title,
                            body=body,
                            notification_type=notification_type,
                            data_payload=data_payload,
                            fcm_token=fcm_token,
                            delay_seconds=0,  # Send immediately for check-in reminders
                            recipient_user_id=user_id
                        )
                        
                        notifications_sent += 1
                        self.stdout.write(f"  ✓ [{index}/{len(late_employees)}] Notification sent to {full_name} (ID: {employee_id})")
                        
                        # Add delay between notifications to avoid rate limiting (1.5 seconds)
                        # Skip delay for the last notification
                        if index < len(late_employees):
                            time_module.sleep(1.5)
                        
                    except Exception as e:
                        failed_notifications += 1
                        self.stderr.write(f"  ✗ [{index}/{len(late_employees)}] Failed to send notification to {full_name} (ID: {employee_id}): {str(e)}")

                self.stdout.write(self.style.SUCCESS(
                    f"Process complete: {notifications_sent} sent, {failed_notifications} failed out of {len(late_employees)} total."
                ))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"An error occurred: {e}"))

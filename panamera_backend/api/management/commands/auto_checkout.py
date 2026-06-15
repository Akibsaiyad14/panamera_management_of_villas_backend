from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from api.utils import execute_query, log_activity_raw  # Adjust the import based on your project structure


class Command(BaseCommand):
    help = 'Automatically checks out employees who are 5 hours past their shift end time.'

    @transaction.atomic
    def handle(self, *args, **options):
        now = datetime.now()
        self.stdout.write(self.style.SUCCESS(f"[{now}] Running automatic checkout process..."))

        try:
            query = """
                SELECT
                    at.id AS "attendanceId",
                    at."labourUserId" AS "employeeId",
                    at."checkInTime",
                    at.date,
                    at."breakInTime",
                    at."breakOutTime",
                    u. "fullName" AS "employeeName",
                    s."startTime" AS "shiftStartTime",
                    s."endTime" AS "shiftEndTime",
                    s."shiftName"
                FROM attendance at
                JOIN public."user" u ON at."labourUserId" = u.id
                JOIN shifts s ON at."assignedShiftAtCheckInId" = s."shiftId"
                WHERE at."checkOutTime" IS NULL
                  AND at."checkInTime" IS NOT NULL
                  AND at."isDeleted" = 0
                  AND s."isDeleted" = 0
                  AND NOW() >= (at.date + s."endTime" + INTERVAL '5 hours')
            """

            records_to_checkout = execute_query(query, many=True)

            if not records_to_checkout:
                self.stdout.write("No employees to auto-checkout at this time.")
                return

            self.stdout.write(self.style.WARNING(f"Found {len(records_to_checkout)} employee(s) to auto-checkout."))

            for record in records_to_checkout:
                self.process_checkout(record)

            self.stdout.write(self.style.SUCCESS("Automatic checkout process completed successfully."))

        except Exception as e:
            raise CommandError(f"An error occurred during the auto-checkout process: {e}")

    def process_checkout(self, record):
        attendance_id = record['attendanceId']
        employee_name = record['employeeName']
        check_in_time = record['checkInTime']
        shift_name = record['shiftName']

        # Define the automatic checkout time as 5 hours past the shift end
        shift_end_dt = datetime.combine(record['date'], record['shiftEndTime'])
        auto_checkout_time = shift_end_dt + timedelta(hours=5)

        # 1. Calculate break duration
        break_duration = timedelta(0)
        if record.get('breakInTime') and record.get('breakOutTime'):
            break_in = record['breakInTime']
            break_out = record['breakOutTime']
            break_duration = break_out - break_in

        # 2. Calculate total hours worked, subtracting break time
        net_work_duration = (auto_checkout_time - check_in_time) - break_duration
        total_working_hours = round(max(net_work_duration.total_seconds() / 3600, 0), 2)

        # 3. Calculate shift duration and overtime
        shift_start_dt = datetime.combine(record['date'], record['shiftStartTime'])

        # Handle overnight shifts where end time is next day
        if shift_end_dt <= shift_start_dt:
            shift_end_dt += timedelta(days=1)

        shift_duration_hours = (shift_end_dt - shift_start_dt).total_seconds() / 3600

        overtime_hours = 0
        overtime_threshold_dt = shift_end_dt + timedelta(minutes=30)
        if auto_checkout_time > overtime_threshold_dt:
            overtime_hours = round(max(0, (auto_checkout_time - shift_end_dt).total_seconds() / 3600), 2)

        # 4. Calculate regular hours
        regular_hours = total_working_hours - overtime_hours

        # 5. Determine attendance status
        early_checkout_threshold = shift_end_dt - timedelta(minutes=30)
        early_checkout_threshold_hours = (early_checkout_threshold - shift_start_dt).total_seconds() / 3600

        if overtime_hours > 0:
            attendance_status = 'Overtime'
        elif total_working_hours == 0:
            attendance_status = 'Absent'
        elif total_working_hours < (shift_duration_hours / 2) or (total_working_hours < early_checkout_threshold_hours):
            attendance_status = 'Halfday'
        elif total_working_hours <= shift_duration_hours:
            attendance_status = 'Normal'
        else:
            attendance_status = 'Normal'

        overtime_status = 1 if overtime_hours > 0 else 0

        # 6. Update the attendance record
        update_query = """
            UPDATE attendance
            SET "checkOutTime" = %s, "effectiveCheckoutTime" = %s, "calculatedRegularHours" = %s,
                "overtimeHours" = %s, "attendanceStatus" = %s, "overtimeStatus" = %s,
                status = %s, "updatedAt" = NOW(), "checkoutType" = %s
            WHERE id = %s
        """
        params = [
            auto_checkout_time, auto_checkout_time, regular_hours, overtime_hours,
            attendance_status, overtime_status, 'COMPLETED', 'AUTOMATIC', attendance_id
        ]

        execute_query(update_query, params, fetch=False)
        self.stdout.write(f"  - Successfully checked out attendance record ID: {attendance_id} with status '{attendance_status}'")

        log_activity_raw(
            request=None,
            category='AutoCheckoutCron',
            action='AutoCheckout',
            performer=None,  # System action
            target_employee_name=employee_name,
            details={
                'attendanceId': attendance_id,
                'shiftName': shift_name,
                'autoCheckoutTime': auto_checkout_time,
                'attendanceStatus': attendance_status,
                'totalHours': total_working_hours,
                'reason': 'Employee did not check out within 5 hours of shift end.'
            }
        )

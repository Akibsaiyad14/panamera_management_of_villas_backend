from django.core.management.base import BaseCommand
from django.db import connection
from datetime import datetime, timedelta
from api.constants import (
    LEAVE_TYPE_EMERGENCY,
    LEAVE_TYPE_ANNUAL,
    LEAVE_TYPE_SICK,
    LEAVE_STATUS_HR_APPROVED,
    LEAVE_STATUS_REJECTED
)


class Command(BaseCommand):
    help = "Finds active employees whose shift for the previous day has ended without an attendance record and marks them as 'Absent', but only if their role permits mobile attendance."

    def handle(self, *args, **options):
        # We check for shifts that ended up until the time the script runs.
        # Running at 3 AM covers all shifts from the previous day.
        target_date = (datetime.now() - timedelta(days=1)).date()
        target_date_str = target_date.strftime('%Y-%m-%d')
        
        # We need the current time to compare against shift end times.
        now_datetime = datetime.now()

        # Check if target date is Sunday (weekday() returns 6 for Sunday)
        is_sunday = target_date.weekday() == 6

        if is_sunday:
            self.stdout.write(f"Target date {target_date_str} is a Sunday. Checking for approved leaves and marking attendance accordingly.")
            try:
                with connection.cursor() as cursor:
                    # Find all active employees who don't have an attendance record for this Sunday
                    find_employees_query = """
                        SELECT u.id, u."shiftId"
                        FROM public."user" u
                        LEFT JOIN public.attendance a ON u.id = a."labourUserId" AND a."date" = %s
                        WHERE
                            u."employmentStatus" = 'Active'
                            AND u."isDeleted" = 0
                            AND a.id IS NULL
                    """
                    
                    cursor.execute(find_employees_query, [target_date_str])
                    employee_records = cursor.fetchall()

                    if not employee_records:
                        self.stdout.write(self.style.SUCCESS("No employees need attendance records. Process complete."))
                        return

                    self.stdout.write(f"Found {len(employee_records)} employees to process. Checking for leaves...")

                    # Check for leaves and insert appropriate attendance records
                    check_leave_query = """
                        SELECT "leaveType", "leaveStatus"
                        FROM public."leaveApplication"
                        WHERE "employeeId" = (SELECT "employeeId" FROM public."user" WHERE id = %s)
                        AND "isDeleted" = 0
                        AND %s BETWEEN "startDate" AND "endDate"
                        ORDER BY "createdAt" DESC
                        LIMIT 1;
                    """
                    
                    insert_query = """
                        INSERT INTO public.attendance
                        ("labourUserId", "date", "attendanceStatus", "calculatedRegularHours", "assignedShiftAtCheckInId", "isDeleted", "createdAt", "updatedAt")
                        VALUES (%s, %s, %s, %s, %s, 0, NOW(), NOW());
                    """
                    
                    leave_type_to_status = {
                        LEAVE_TYPE_EMERGENCY: 'Emergency Leave',
                        LEAVE_TYPE_ANNUAL: 'Annual Leave',
                        LEAVE_TYPE_SICK: 'Sick Leave'
                    }
                    
                    day_off_count = 0
                    leave_count = 0
                    
                    for user_id, shift_id in employee_records:
                        # Check if user has an approved leave for this Sunday
                        cursor.execute(check_leave_query, [user_id, target_date_str])
                        leave_record = cursor.fetchone()
                        
                        attendance_status = 'Day Off'  # Default for Sunday
                        total_hours = 0
                        
                        if leave_record:
                            leave_type, leave_status = leave_record
                            
                            # If leave is approved by HR, use the leave type as attendance status
                            if leave_status == LEAVE_STATUS_HR_APPROVED:
                                attendance_status = leave_type_to_status.get(leave_type, 'Day Off')
                                total_hours = 0
                                leave_count += 1
                            else:
                                # If leave is not HR-approved (pending/rejected), mark as Day Off
                                day_off_count += 1
                        else:
                            # No leave application, mark as Day Off
                            day_off_count += 1
                        
                        cursor.execute(insert_query, [user_id, target_date_str, attendance_status, total_hours, shift_id])

                    self.stdout.write(self.style.SUCCESS(
                        f"Successfully created {len(employee_records)} attendance records for Sunday: "
                        f"{day_off_count} Day Off, {leave_count} on approved leave."
                    ))

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"An error occurred while marking Sunday attendance: {e}"))
            return

        # If not Sunday, proceed with normal absent marking logic
        self.stdout.write(f"Starting SHIFT-AWARE process to mark absentees for date: {target_date_str}")
        self.stdout.write(f"Current server time is: {now_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            with connection.cursor() as cursor:
                # This query is the core of the new logic. It has been updated to include a permission check.
                #
                # It finds users who:
                # 1. Are 'Active' and not deleted.
                # 2. Have a shift assigned ("shiftId" IS NOT NULL).
                # 3. DO NOT have an attendance record for the target date.
                # 4. The END of their shift has already passed.
                # 5. NEW: Their assigned role has the 'mobile_attendance_my_attendance' functionality key.
                #
                # We construct the shift's end datetime on the fly.
                # Example: If target_date is '2023-10-26' and shift ends at '06:00':
                #   - If startTime > endTime (e.g., 22:00-06:00), the end datetime is on the NEXT day: '2023-10-27 06:00'.
                #   - If startTime <= endTime (e.g., 09:00-17:00), the end datetime is on the SAME day: '2023-10-26 17:00'.
                
                find_absentees_query = """
                    SELECT u.id, u."shiftId"
                    FROM public."user" u
                    JOIN public.shifts s ON u."shiftId" = s."shiftId"
                    -- NEW: Join with the userrole table to check permissions
                    JOIN public.userrole ur ON u."roleId" = ur."roleId"
                    LEFT JOIN public.attendance a ON u.id = a."labourUserId" AND a."date" = %s
                    WHERE
                        u."employmentStatus" = 'Active'
                        AND u."isDeleted" = 0
                        AND s."isDeleted" = 0
                        AND u."shiftId" IS NOT NULL
                        AND a.id IS NULL
                        -- NEW: Check if the role's functionalityKey contains 'mobile_attendance_my_attendance'.
                        -- We cast the text field to jsonb and use the '@>' (contains) operator.
                        AND ur."functionalityKey"::jsonb @> '"mobile_attendance_my_attendance"'::jsonb
                        AND (
                            -- This is the crucial shift-aware time check
                            CASE
                                -- Night shift crossing midnight (e.g., 22:00 to 06:00)
                                WHEN s."startTime" > s."endTime" THEN
                                    (%s::date + interval '1 day' + s."endTime") < %s
                                -- Normal day shift (e.g., 09:00 to 17:00)
                                ELSE
                                    (%s::date + s."endTime") < %s
                            END
                        );
                """
                
                # The parameters must match the placeholders in the query.
                # No new parameters are needed for the permission check.
                params = [
                    target_date_str,         # For the attendance join
                    target_date_str,         # For the night shift end time calculation
                    now_datetime,            # For comparing against the current time
                    target_date_str,         # For the day shift end time calculation
                    now_datetime             # For comparing against the current time
                ]

                cursor.execute(find_absentees_query, params)
                # absentee_ids = [row[0] for row in cursor.fetchall()]
                absentee_ids = cursor.fetchall()

                if not absentee_ids:
                    self.stdout.write(self.style.SUCCESS("No absent employees found whose shifts have ended and have attendance permission. Process complete."))
                    return

                self.stdout.write(f"Found {len(absentee_ids)} absent employees with attendance permission. Creating records...")

                # Check for leaves and determine attendance status accordingly
                check_leave_query = """
                    SELECT "leaveType", "leaveStatus"
                    FROM public."leaveApplication"
                    WHERE "employeeId" = (SELECT "employeeId" FROM public."user" WHERE id = %s)
                    AND "isDeleted" = 0
                    AND %s BETWEEN "startDate" AND "endDate"
                    ORDER BY "createdAt" DESC
                    LIMIT 1;
                """
                
                insert_query = """
                    INSERT INTO public.attendance
                    ("labourUserId", "date", "attendanceStatus", "calculatedRegularHours", "assignedShiftAtCheckInId", "isDeleted", "createdAt", "updatedAt")
                    VALUES (%s, %s, %s, %s, %s, 0, NOW(), NOW());
                """
                
                leave_type_to_status = {
                    LEAVE_TYPE_EMERGENCY: 'Emergency Leave',
                    LEAVE_TYPE_ANNUAL: 'Annual Leave',
                    LEAVE_TYPE_SICK: 'Sick Leave'
                }
                
                for user_id, shift_id in absentee_ids:
                    # Check if user has a leave for this date
                    cursor.execute(check_leave_query, [user_id, target_date_str])
                    leave_record = cursor.fetchone()
                    
                    attendance_status = 'Absent'  # Default
                    total_hours = 0
                    
                    if leave_record:
                        leave_type, leave_status = leave_record
                        
                        # If leave is approved by HR, use the leave type as attendance status
                        if leave_status == LEAVE_STATUS_HR_APPROVED:
                            attendance_status = leave_type_to_status.get(leave_type, 'Absent')
                            total_hours = 0  # No working hours for approved leaves
                        # If leave is rejected, mark as Absent
                        elif leave_status == LEAVE_STATUS_REJECTED:
                            attendance_status = 'Absent'
                            total_hours = 0
                    
                    cursor.execute(insert_query, [user_id, target_date_str, attendance_status, total_hours, shift_id])

                self.stdout.write(self.style.SUCCESS(f"Successfully created {len(absentee_ids)} attendance records (including leave-based statuses)."))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"An error occurred: {e}"))

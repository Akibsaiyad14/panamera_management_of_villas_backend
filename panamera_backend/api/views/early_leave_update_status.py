from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from api.utils import success_response, error_response, execute_query, log_activity_raw
from api.constants import (STATUS_NA, STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)



class UpdateEarlyReasonStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            attendance_id = request.data.get("attendanceId")
            new_status = request.data.get("status")

            if attendance_id is None or new_status is None:
                return error_response(
                    message="attendanceId and status are required.",
                    status_code=drf_status.HTTP_400_BAD_REQUEST
                )

            try:
                new_status = int(new_status)
            except (ValueError, TypeError):
                return error_response(
                    message="Status must be a valid integer.",
                    status_code=drf_status.HTTP_400_BAD_REQUEST
                )

            if new_status not in [STATUS_NA, STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED]:
                return error_response(
                    message="Invalid status value. Allowed values: 0, 1, 2, 3.",
                    status_code=drf_status.HTTP_400_BAD_REQUEST
                )

            # Validate attendance record exists
            record = execute_query(
                """SELECT "id","labourUserId", "assignedShiftAtCheckInId" ,"attendanceStatus", "date" FROM "attendance" WHERE "id" = %s AND "isDeleted" = 0""",
                [attendance_id],
                many=False
            )

            if not record:
                return error_response(
                    message="Attendance record not found.",
                    status_code=drf_status.HTTP_404_NOT_FOUND
                )


            target_user_id = record[0]['labourUserId']
            target_shift_id = record[0]['assignedShiftAtCheckInId']
            attendance_date = record[0]['date']

            # Fetch the human-readable employeeId for the target user
            user_info = execute_query('SELECT "employeeId", "fullName" FROM "user" WHERE id = %s', [target_user_id], many=False)
            if not user_info:
                return error_response("Target employee for this record not found.", drf_status.HTTP_404_NOT_FOUND)

            target_employee_name = user_info[0]['fullName']

            set_clauses = ['"earlyReasonStatus" = %s']
            params = [new_status]

            if new_status == STATUS_APPROVED:
                set_clauses.append('"attendanceStatus" = %s')
                params.append('Normal')
                
                # When approving early leave, credit full shift hours and set times to shift times
                if target_shift_id:
                    shift_info = execute_query(
                        'SELECT "startTime", "endTime" FROM "shifts" WHERE "shiftId" = %s AND "isDeleted" = 0',
                        [target_shift_id],
                        many=False
                    )
                    if shift_info:
                        from datetime import datetime, timedelta, time
                        start_time = shift_info[0]['startTime']
                        end_time = shift_info[0]['endTime']
                        
                        # Convert time/timedelta to datetime for the attendance date
                        if isinstance(start_time, timedelta):
                            start_seconds = int(start_time.total_seconds())
                            shift_start_time = time(start_seconds // 3600, (start_seconds % 3600) // 60, start_seconds % 60)
                        else:
                            shift_start_time = start_time
                        
                        if isinstance(end_time, timedelta):
                            end_seconds = int(end_time.total_seconds())
                            shift_end_time = time(end_seconds // 3600, (end_seconds % 3600) // 60, end_seconds % 60)
                        else:
                            shift_end_time = end_time
                        
                        # Create full datetime objects
                        checkin_datetime = datetime.combine(attendance_date, shift_start_time)
                        checkout_datetime = datetime.combine(attendance_date, shift_end_time)
                        
                        # Handle overnight shifts
                        if shift_end_time < shift_start_time:
                            checkout_datetime += timedelta(days=1)
                        
                        # Calculate full shift hours
                        time_diff = checkout_datetime - checkin_datetime
                        full_shift_hours = time_diff.total_seconds() / 3600.0
                        
                        set_clauses.extend(['"checkInTime" = %s', '"checkOutTime" = %s', '"calculatedRegularHours" = %s'])
                        params.extend([checkin_datetime, checkout_datetime, full_shift_hours])

            # Note: if REJECTED — do nothing else, earlyReason and attendanceStatus remain as is

            set_clause_string = ", ".join(set_clauses)
            params.append(attendance_id)

            update_query = f"""
                UPDATE "attendance"
                SET {set_clause_string}, "updatedAt" = NOW()
                WHERE "id" = %s
            """

            execute_query(update_query, params, fetch=False)


            action_map = {
                STATUS_APPROVED: 'Approved',
                STATUS_REJECTED: 'Rejected'
            }
            log_action = action_map.get(new_status, 'UpdateStatus')

            # Call the logging function with all pre-fetched details
            log_activity_raw(
                request=request,
                category='EarlyLeave',  # This category is for early leave requests
                action=log_action,
                performer=request.user, # The logged-in admin
                target_employee_name=target_employee_name, # Pass the fetched employee fullName
                target_shift_id=target_shift_id, # Pass the fetched shiftId
                details={
                    'updatedAttendanceId': attendance_id,
                    'newStatus': new_status
                }
            )


            return success_response(
                message="Early reason status updated successfully.",
                status_code=drf_status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(
                message=f"Error updating early reason status: {str(e)}",
                status_code=drf_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

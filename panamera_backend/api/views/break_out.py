from datetime import datetime
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.messages import *
from api.utils import success_response, error_response, log_activity_raw
from api.utils import execute_query  # Assuming you have this utility function for executing SQL queries



class BreakOutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        user_id = request.user.id

        try:
            break_out_time_str = request.data.get("dateTime")
            break_out_time = datetime.strptime(break_out_time_str, "%Y-%m-%d %H:%M:%S")
            today = break_out_time.date()
        except Exception as e:
            return error_response(message=f"{INVALID_DATETIME_FORMAT}: {str(e)}", status_code=400)

        # 1. Find the user's attendance record that has an active break
        attendance = execute_query("""
            SELECT id, "breakInTime", "breakOutTime"
            FROM attendance
            WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
        """, [user_id, today])

        # 2. VALIDATIONS
        if not attendance:
            return error_response(message=NO_ATTENDANCE_RECORD_TODAY, status_code=404)

        attendance_record = attendance[0]
        attendance_id = attendance_record['id']

        if not attendance_record['breakInTime']:
            return error_response(message=BREAK_NOT_STARTED, status_code=400)

        if attendance_record['breakOutTime']:
            return error_response(message=BREAK_ALREADY_ENDED, status_code=400)

        # 3. ACTION: Update the attendance record with the break-out time
        execute_query("""
            UPDATE attendance
            SET "breakOutTime" = %s,
                status = %s, -- Set status back to CHECKED_IN
                "updatedAt" = %s
            WHERE id = %s
        """, [break_out_time, 'CHECKED_IN', timezone.now(), attendance_id])

        log_activity_raw(
            request=request,
            category='Attendance',
            action='BreakOut',
            performer=user,
            target_employee_name=user.fullName,
            break_out_time= break_out_time_str,
            details={
                'attendanceId': attendance_id,
                'breakInTime': break_out_time_str
            }
        )



        # 4. RESPONSE
        return success_response(
            data={
                'attendanceId': attendance_id,
                'breakOutTime': break_out_time.strftime("%Y-%m-%d %H:%M:%S")
            },
            message=BREAK_ENDED_SUCCESSFULLY,
            status_code=200
        )

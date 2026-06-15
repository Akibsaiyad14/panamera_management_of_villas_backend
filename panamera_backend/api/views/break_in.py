# views.py
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from datetime import datetime, date
from django.utils import timezone
from api.messages import *
from api.utils import success_response, error_response, execute_query, log_activity_raw
# Assuming you have these helpers from your original code
# from .utils import success_response, error_response, execute_query

class BreakInView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        user_id = request.user.id

        try:
            # Frontend should provide the time the break is starting
            break_in_time_str = request.data.get("dateTime")
            break_in_time = datetime.strptime(break_in_time_str, "%Y-%m-%d %H:%M:%S")
            today = break_in_time.date()
        except Exception as e:
            return error_response(message=f"{INVALID_DATETIME_FORMAT}: {str(e)}", status_code=400)

        # 1. Find the user's active attendance record for today
        #    This is the most crucial step
        attendance = execute_query("""
            SELECT id, "checkInTime", "checkOutTime", "breakInTime"
            FROM attendance
            WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
        """, [user_id, today])

        # 2. VALIDATIONS
        if not attendance:
            return error_response(message=NOT_CHECKED_IN_TODAY, status_code=400)

        attendance_record = attendance[0]
        attendance_id = attendance_record['id']

        if attendance_record['checkOutTime']:
            return error_response(message=ALREADY_CHECKED_OUT_FOR_DAY, status_code=400)

        if attendance_record['breakInTime']:
            return error_response(message=BREAK_ALREADY_STARTED, status_code=400)

        # 3. ACTION: Update the attendance record with the break-in time
        execute_query("""
            UPDATE attendance
            SET "breakInTime" = %s,
                status = %s,
                "updatedAt" = %s
            WHERE id = %s
        """, [break_in_time, 'ON_BREAK', timezone.now(), attendance_id])

        log_activity_raw(
            request=request,
            category='Attendance',
            action='BreakIn',
            performer=user,
            target_employee_name=user.fullName,
            break_in_time= break_in_time_str,
            details={
                'attendanceId': attendance_id,
                'breakInTime': break_in_time_str
            }
        )


        # 4. RESPONSE
        return success_response(
            data={
                'attendanceId': attendance_id,
                'breakInTime': break_in_time.strftime("%Y-%m-%d %H:%M:%S")
            },
            message=BREAK_STARTED_SUCCESSFULLY,
            status_code=200
        )

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from django.conf import settings
from django.utils import timezone
from api.utils import success_response, error_response, execute_query, send_early_leave_notification
import os
from django.db import transaction


class UpdateEarlyReasonView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic # Ensures both the DB update and notification logic are a single unit
    def post(self, request):
        try:
            attendance_id = request.data.get("attendanceRecordId")
            early_reason = request.data.get("earlyReason", "").strip() # Use .strip() to remove whitespace

            # Validate that both required fields are present and not empty
            if not attendance_id or not early_reason:
                return error_response(
                    message="attendanceRecordId and a non-empty earlyReason are required.",
                    status_code=drf_status.HTTP_400_BAD_REQUEST
                )

            # Fetch attendance record AND the employee's details needed for the notification in one go.
            record_details = execute_query(
                """
                SELECT a.id, a."labourUserId", u."fullName"
                FROM attendance a
                JOIN "user" u ON a."labourUserId" = u.id
                WHERE a.id = %s AND a."isDeleted" = 0
                """,
                [attendance_id],
                many=False
            )

            if not record_details:
                return error_response(
                    message="Attendance record not found.",
                    status_code=drf_status.HTTP_404_NOT_FOUND
                )

            employee_info = record_details[0]
            employee_id = employee_info['labourUserId']
            employee_name = employee_info['fullName']

            # Update earlyReason and earlyReasonStatus in attendance table.
            # Simplified the query since we've already validated the reason is not empty.
            execute_query(
                """
                UPDATE attendance
                SET "earlyReason" = %s, "earlyReasonStatus" = 1, "updatedAt" = NOW()
                WHERE id = %s
                """,
                [early_reason, attendance_id]
            )

            # --- CORRECTED NOTIFICATION CALL ---
            # After successfully updating the database, send the notification.
            send_early_leave_notification(
                employee_id=employee_id,
                employee_name=employee_name,
                attendance_id=attendance_id,
                early_reason=early_reason
            )

            return success_response(
                message="Early reason updated.",
                data={"attendanceRecordId": attendance_id, "earlyReason": early_reason},
                status_code=200
            )

        except Exception as e:
            # The transaction will be rolled back automatically if any error occurs
            return error_response(
                message=f"An error occurred: {str(e)}",
                status_code=500
            )

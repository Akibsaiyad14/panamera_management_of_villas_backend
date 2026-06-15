from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response, log_activity_raw
from datetime import datetime, timedelta


class UpdateAttendanceStatusView(APIView):
    """
    API to update attendance status for one or multiple attendance records.
    Supports bulk operations.
    For leave types, automatically fills shift hours.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            attendance_status = request.data.get("attendanceStatus")
            attendance_ids = request.data.get("attendanceIds", [])

            # Validate inputs
            if not isinstance(attendance_ids, list) or not attendance_ids:
                return error_response(
                    message=ATTENDANCE_IDS_MUST_BE_NON_EMPTY_LIST,
                    status_code=400
                )

            if not attendance_status:
                return error_response(
                    message="Attendance status is required",
                    status_code=400
                )

            # Validate attendance status length
            if len(attendance_status) > 20:
                return error_response(
                    message="Attendance status must not exceed 20 characters",
                    status_code=400
                )

            placeholders = ','.join(['%s'] * len(attendance_ids))

            # Fetch attendance records by ID only
            fetch_query = f"""
                SELECT a.id,
                       a."labourUserId",
                       a.date,
                       a."assignedShiftAtCheckInId",
                       a."attendanceStatus",
                       u."shiftId"
                FROM attendance a
                LEFT JOIN "user" u ON a."labourUserId" = u.id
                WHERE a.id IN ({placeholders})
                AND a."isDeleted" = 0
            """
            existing_records = execute_query(fetch_query, attendance_ids, many=True)

            if not existing_records:
                return error_response(
                    message="No attendance records found for the provided attendance IDs",
                    status_code=404
                )

            # Check if any record has attendanceStatus = 'overtime' - prevent update
            overtime_records = [
                r for r in existing_records 
                if r.get("attendanceStatus", "").lower() == "overtime"
            ]
            if overtime_records:

                return error_response(
                    message=f"Cannot update attendance records with 'Overtime' status.",
                    status_code=400
                )

            half_day = [
                e for e in existing_records
                if e.get("attendanceStatus", "").lower() == "halfday"
            ]
            if half_day:
                
                return error_response(
                    message=f"Cannot update attendance records with 'Halfday' status.",
                    status_code=400
                )


            # Leave types
            leave_types = [
                    'Normal',
                    'Sick Leave',        # SL
                    'Annual Leave',      # AL
                    'Paid Leave',        # PL
                    'Paid Holiday',      # PH
                    'Public Holiday',    # PH
                    'Day Off'
            ]
            is_leave = attendance_status in leave_types

            updated_records = []

            if is_leave:
                for record in existing_records:
                    attendance_id = record["id"]
                    shift_id = record.get("assignedShiftAtCheckInId") or record.get("shiftId")
                    attendance_date = record["date"]

                    if shift_id:
                        shift_query = """
                            SELECT "startTime", "endTime"
                            FROM shifts
                            WHERE "shiftId" = %s AND "isDeleted" = 0
                        """
                        shift_result = execute_query(shift_query, [shift_id], many=False)

                        if shift_result:
                            shift = shift_result[0]
                            start_time = shift["startTime"]
                            end_time = shift["endTime"]

                            checkin_dt = datetime.combine(attendance_date, start_time)
                            checkout_dt = datetime.combine(attendance_date, end_time)

                            # Overnight shift handling
                            if end_time <= start_time:
                                checkout_dt += timedelta(days=1)

                            shift_hours = (
                                checkout_dt - checkin_dt
                            ).total_seconds() / 3600

                            update_query = """
                                UPDATE attendance
                                SET "attendanceStatus" = %s,
                                    "checkInTime" = %s,
                                    "checkOutTime" = %s,
                                    "effectiveCheckoutTime" = %s,
                                    "calculatedRegularHours" = %s,
                                    "overtimeHours" = 0,
                                    "updatedAt" = NOW()
                                WHERE id = %s AND "isDeleted" = 0
                                RETURNING id, "labourUserId"
                            """
                            result = execute_query(
                                update_query,
                                [
                                    attendance_status,
                                    checkin_dt,
                                    checkout_dt,
                                    checkout_dt,
                                    round(shift_hours, 2),
                                    attendance_id
                                ],
                                many=False
                            )
                            if result:
                                updated_records.extend(result)
                    else:
                        # No shift → update status only
                        update_query = """
                            UPDATE attendance
                            SET "attendanceStatus" = %s,
                                "updatedAt" = NOW()
                            WHERE id = %s AND "isDeleted" = 0
                            RETURNING id, "labourUserId"
                        """
                        result = execute_query(
                            update_query,
                            [attendance_status, attendance_id],
                            many=False
                        )
                        if result:
                            updated_records.extend(result)
            elif attendance_status.lower() == 'absent':
                # Special handling for Absent - clear all check-in/check-out fields
                update_query = f"""
                    UPDATE attendance
                    SET "attendanceStatus" = %s,
                        "checkInTime" = NULL,
                        "checkOutTime" = NULL,
                        "effectiveCheckoutTime" = NULL,
                        "calculatedRegularHours" = NULL,
                        "overtimeHours" = 0.0,
                        "updatedAt" = NOW()
                    WHERE id IN ({placeholders})
                    AND "isDeleted" = 0
                    RETURNING id, "labourUserId"
                """
                updated_records = execute_query(
                    update_query,
                    [attendance_status] + attendance_ids,
                    many=True
                )
            else:
                # Non-leave status bulk update
                update_query = f"""
                    UPDATE attendance
                    SET "attendanceStatus" = %s,
                        "updatedAt" = NOW()
                    WHERE id IN ({placeholders})
                    AND "isDeleted" = 0
                    RETURNING id, "labourUserId"
                """
                updated_records = execute_query(
                    update_query,
                    [attendance_status] + attendance_ids,
                    many=True
                )

            if not updated_records:
                return error_response(
                    message="Failed to update attendance records",
                    status_code=500
                )

            updated_user_ids = [r["labourUserId"] for r in updated_records]
            updated_user_name_query = """SELECT "fullName" FROM "user" WHERE id = %s"""
            for r in updated_records:
                user_name_result = execute_query(updated_user_name_query, [r["labourUserId"]], many=False)
                if user_name_result and len(user_name_result) > 0:
                    r["userName"] = user_name_result[0].get("fullName", "N/A")
                else:
                    r["userName"] = "N/A"
            update_count = len(updated_user_ids)

            # Logging
            if update_count == 1:
                log_action = "UpdateAttendanceStatus"
                log_details = {
                    "userId": updated_user_ids[0],
                    "userName": updated_records[0]["userName"],
                    "attendanceId": updated_records[0]["id"],
                    "newStatus": attendance_status,
                    "isLeave": is_leave
                }
            else:
                log_action = "BulkUpdateAttendanceStatus"
                log_details = {
                    "userIds": updated_user_ids,
                    "attendanceIds": attendance_ids,
                    "count": update_count,
                    "newStatus": attendance_status,
                    "isLeave": is_leave
                }

            log_activity_raw(
                request=request,
                category="Attendance",
                action=log_action,
                performer=request.user,
                target_employee_name=None,
                target_shift_id=None,
                details=log_details
            )

            return success_response(
                data={
                    "updatedUserIds": updated_user_ids,
                    "count": update_count,
                    "attendanceStatus": attendance_status,
                    "isLeave": is_leave
                },
                message="Attendance status updated successfully."
            )

        except Exception as e:
            return error_response(
                message=f"Error updating attendance status: {str(e)}",
                status_code=500
            )

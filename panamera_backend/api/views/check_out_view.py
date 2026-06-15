from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from datetime import datetime, timedelta
from django.db import transaction, connection
from django.utils import timezone
from rest_framework import status as http_status
from api.utils import success_response, error_response, execute_query, _calculate_attendance_details, send_overtime_notification, get_current_dubai_time, log_activity_raw, _send_notification
import logging
import traceback

logger = logging.getLogger(__name__)


class CheckOutView(APIView):
    """
    Handles the check-out process for a user, operating entirely in naive Dubai time.
    Allows overriding automatic checkout time if `isOfflineData` flag is True and a manual checkout is being synced.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        user_id = request.user.id
        user_name = getattr(request.user, 'fullName', 'Unknown')
        
        # Log all check-out requests
        logger.info(f"[CHECKOUT REQUEST] User {user_id} - Request data: {request.data}")

        try:
            checkout_time_str = request.data.get("dateTime")
            check_out_device_id = request.data.get("checkOutDeviceId")
            latitude = float(request.data.get("latitude"))
            longitude = float(request.data.get("longitude"))
            is_offline = str(request.data.get("isOfflineData", "false")).lower() == "true"

            if not (checkout_time_str and check_out_device_id):
                logger.error(f"[CHECKOUT ERROR] User {user_id} - Missing required fields. Received: dateTime={checkout_time_str}, deviceId={check_out_device_id}")
                return error_response(message=MISSING_REQUIRED_FIELDS, status_code=http_status.HTTP_400_BAD_REQUEST)

            checkout_time = datetime.strptime(checkout_time_str, "%Y-%m-%d %H:%M:%S")
            print(checkout_time)
            today = checkout_time.date()
            print(today)

        except (ValueError, TypeError, AttributeError) as e:
            error_msg = INVALID_FORMAT_FOR_INPUTS + f" Error: {e}"
            logger.error(f"[CHECKOUT ERROR] User {user_id} - {error_msg}")
            logger.error(f"[CHECKOUT ERROR] Request data: {request.data}")
            logger.error(f"[CHECKOUT ERROR] Stack trace:\n{traceback.format_exc()}")
            return error_response(message=error_msg, status_code=http_status.HTTP_400_BAD_REQUEST)

        try:
            # Fetch today's attendance record, including checkoutType
            record_result = execute_query("""
                SELECT id, "checkInTime", "checkOutTime", "assignedShiftAtCheckInId",
                       "breakInTime", "breakOutTime", "checkoutType"
                FROM attendance
                WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
            """, [user_id, today], many=False)

            if not record_result:
                return error_response(message=NO_CHECK_IN_FOUND_FOR_TODAY, status_code=http_status.HTTP_400_BAD_REQUEST)

            record = record_result[0]
            check_in_time = record['checkInTime']
            existing_checkout_time = record.get('checkOutTime')
            existing_checkout_type = record.get('checkoutType') # Get the existing checkout type
            attendance_status_db = record.get("attendanceStatus")

            if existing_checkout_time and not is_offline:
                return error_response(ALREADY_CHECKED_OUT, http_status.HTTP_400_BAD_REQUEST)

            if record["breakInTime"] and not record["breakOutTime"]:
                return error_response(CANNOT_CHECK_OUT_ON_BREAK, http_status.HTTP_400_BAD_REQUEST)

            if check_in_time and checkout_time <= check_in_time:
                return error_response(
                    CLOCK_OUT_TIME_MUST_BE_AFTER_CLOCK_IN_TIME,
                    http_status.HTTP_400_BAD_REQUEST
                )

            # 🔍 Determine override reason
            if is_offline:
                if existing_checkout_type == 'AUTOMATIC':
                    override_reason = 'AUTOMATIC_OVERRIDE'
                elif attendance_status_db == 'Absent':
                    override_reason = 'ABSENT_OVERRIDE'
                else:
                    override_reason = 'OFFLINE_CHECKOUT'
            else:
                override_reason = 'ONLINE_CHECKOUT'

            if record.get('breakInTime') and not record.get('breakOutTime'):
                return error_response(message=CANNOT_CHECK_OUT_ON_BREAK, status_code=http_status.HTTP_400_BAD_REQUEST)

            if checkout_time <= check_in_time:
                return error_response(message=CLOCK_OUT_TIME_MUST_BE_AFTER_CLOCK_IN_TIME, status_code=http_status.HTTP_400_BAD_REQUEST)

            # Calculate break duration
            break_duration = timedelta(0)
            if record.get('breakInTime') and record.get('breakOutTime'):
                break_in_time = record['breakInTime']
                break_out_time = record['breakOutTime']
                break_duration = break_out_time - break_in_time

            # Fetch shift details
            shift_id = record.get('assignedShiftAtCheckInId')
            shift_record = None
            if shift_id:
                shift_result = execute_query("""
                    SELECT "startTime", "endTime" FROM shifts WHERE "shiftId" = %s
                """, [shift_id], many=False)
                shift_record = shift_result[0] if shift_result else None

            # Attendance calculations
            calc_details = _calculate_attendance_details(check_in_time, checkout_time, shift_record, break_duration)

            total_working_hours = calc_details['total_working_hours']
            regular_hours = calc_details['regular_hours']
            overtime_hours = calc_details['overtime_hours']
            attendance_status = calc_details['attendance_status']

            if attendance_status == 'Halfday':
                execute_query("""
                UPDATE attendance
                SET "earlyReasonStatus"=1, "earlyReason"= ' '
                WHERE id=%s
            """, [
                record["id"]
                ])

            if is_offline and attendance_status_db == 'Absent':
                attendance_status = 'Normal'

            overtime_status = 1 if overtime_hours > 0 else 0

            # Get current Dubai time (as naive datetime) for "updatedAt" and "createdAt" fields
            now_dubai = get_current_dubai_time()
             

            # Update attendance record
            execute_query("""
                UPDATE attendance
                SET "checkOutTime"=%s,
                    "effectiveCheckoutTime"=%s,
                    "calculatedRegularHours"=%s,
                    "overtimeHours"=%s,
                    "checkOutLatitude"=%s,
                    "checkOutLongitude"=%s,
                    "attendanceStatus"=%s,
                    "overtimeStatus"=%s,
                    status='COMPLETED',
                    "checkOutDeviceId"=%s,
                    "checkoutType"='MANUAL',
                    "updatedAt"=%s
                WHERE id=%s
            """, [
                checkout_time, checkout_time, regular_hours, overtime_hours,
                latitude, longitude, attendance_status, overtime_status,
                check_out_device_id, now_dubai, record["id"]
            ])

            overtime_request_id = None
            if overtime_hours > 0:
                existing_ot = execute_query("""
                    SELECT "overtimeId"
                    FROM "overtimeRequests"
                    WHERE "attendanceRecordId" = %s
                """, [record["id"]], many=False)

                if existing_ot:
                    overtime_request_id = existing_ot[0]["overtimeId"]
                    logger.info(
                        f"[OVERTIME] Reused existing overtime request {overtime_request_id} "
                        f"for attendance {record['id']}"
                    )
                else:
                    ot = execute_query("""
                        INSERT INTO "overtimeRequests"
                        ("attendanceRecordId", "actualCheckoutTime", "createdAt", "updatedAt")
                        VALUES (%s,%s,%s,%s)
                        RETURNING "overtimeId"
                    """, [record["id"], checkout_time, now_dubai, now_dubai], fetch=True)

                    if ot:
                        overtime_request_id = ot[0]["overtimeId"]
                        send_overtime_notification(
                            employee_id=user_id,
                            employee_name=user_name,
                            overtime_hours_decimal=overtime_hours,
                            attendance_id=record['id']
                        )

            action_map = {
                'AUTOMATIC_OVERRIDE': 'OfflineCheckOutSyncOverride',
                'ABSENT_OVERRIDE': 'OfflineCheckOut_AbsentFixed',
                'OFFLINE_CHECKOUT': 'OfflineCheckOut',
                'ONLINE_CHECKOUT': 'CheckOut'
            }

            log_activity_raw(
                request=request,
                category='Attendance',
                action=action_map[override_reason],
                performer=user,
                target_employee_name=user_name,
                target_shift_id=record["assignedShiftAtCheckInId"],
                details={
                    "checkOutTime": checkout_time_str,
                    "overrideReason": override_reason,
                    "previousCheckoutType": existing_checkout_type,
                    "previousAttendanceStatus": attendance_status_db,
                    "finalAttendanceStatus": attendance_status,
                    "totalWorkingHours": total_working_hours,
                    "overtimeHours": overtime_hours,
                    "isOffline": is_offline
                }
            )

            # 💬 Response message
            message_map = {
                'AUTOMATIC_OVERRIDE': " (Automatic checkout overridden by offline sync).",
                'ABSENT_OVERRIDE': " (Absent attendance corrected by offline sync).",
                'OFFLINE_CHECKOUT': " (Offline checkout synced successfully).",
                'ONLINE_CHECKOUT': ""
            }
            
            # Log successful check-out
            logger.info(f"[CHECKOUT SUCCESS] User {user_id} - Attendance ID: {record['id']}, Time: {checkout_time_str}, OT Hours: {overtime_hours}, Offline: {is_offline}")

            # Store notification in database about successful check-out
            overtime_text = f" with {overtime_hours:.2f} hours of overtime" if overtime_hours > 0 else ""
            _send_notification(
                recipient_user_id=user_id,
                title="Check-Out Successful",
                body=f"You checked out at {checkout_time.strftime('%I:%M %p')}. Total hours: {total_working_hours:.2f}{overtime_text}.",
                notification_type="CHECK_OUT",
                data_payload={
                    'attendanceId': str(record['id']),
                    'checkOutTime': checkout_time_str,
                    'workingHours': str(total_working_hours),
                    'overtimeHours': str(overtime_hours),
                    'type': 'CHECK_OUT'
                },
                fcm_token=None,
                attendance_id=record['id'],
                is_sync=True
            )

            return success_response(
                data={
                    'attendanceId': record['id'],
                    'clockInTime': check_in_time.strftime("%Y-%m-%d %H:%M:%S"),
                    'clockOutTime': checkout_time.strftime("%Y-%m-%d %H:%M:%S"),
                    'workingHours': total_working_hours,
                    'attendanceStatus': attendance_status,
                    'overtimeHours': overtime_hours,
                    'overtimeRequestId': overtime_request_id,
                    'status': 'COMPLETED'
                },
                message=CHECKED_OUT_SUCCESSFULLY + message_map[override_reason],
                status_code=http_status.HTTP_200_OK
            )
        except Exception as e:
            error_msg = f"Unexpected error during check-out: {str(e)}"
            logger.error(f"[CHECKOUT ERROR] User {user_id} - {error_msg}")
            logger.error(f"[CHECKOUT ERROR] Request data: {request.data}")
            logger.error(f"[CHECKOUT ERROR] Attendance record: {record_result if 'record_result' in locals() else 'Not fetched'}")
            logger.error(f"[CHECKOUT ERROR] Stack trace:\n{traceback.format_exc()}")
            return error_response(message="An error occurred during check-out. Please contact support.", status_code=500)

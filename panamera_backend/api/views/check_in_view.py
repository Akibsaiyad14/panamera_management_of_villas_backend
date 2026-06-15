from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from datetime import datetime
from django.utils import timezone
from api.utils import success_response, error_response, execute_query, log_activity_raw, _send_notification
import logging
import traceback

logger = logging.getLogger(__name__)


class CheckInView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        user_id = request.user.id

        try:
            checkin_time_str = request.data.get("dateTime")
            checkin_device_id = request.data.get("checkInDeviceId")
            latitude = request.data.get("latitude")
            longitude = request.data.get("longitude")
            is_offline = str(request.data.get("isOfflineData", "false")).lower() == "true"
            checkin_time = datetime.strptime(checkin_time_str, "%Y-%m-%d %H:%M:%S")
            today = checkin_time.date()
        except Exception as e:
            error_msg = f"Invalid datetime or location: {str(e)}"
            logger.error(f"[CLOCKIN ERROR] User {user_id} - {error_msg}")
            logger.error(f"[CLOCKIN ERROR] Request data: {request.data}")
            logger.error(f"[CLOCKIN ERROR] Stack trace:\n{traceback.format_exc()}")
            return error_response(message=error_msg, status_code=400)

        if not all([checkin_time_str, checkin_device_id, latitude, longitude]):
            logger.error(f"[CLOCKIN ERROR] User {user_id} - Missing required fields. Received: dateTime={checkin_time_str}, deviceId={checkin_device_id}, lat={latitude}, long={longitude}")
            return error_response(message=DATETIME_DEVICE_LAT_LONG_REQUIRED, status_code=400)

        employement_status_query = execute_query('SELECT "employmentStatus" FROM "user" WHERE id = %s', [user_id])
        employement_status = employement_status_query[0].get('employmentStatus') if employement_status_query else None
        if employement_status != 'Active':
            logger.warning(f"[CLOCKIN WARNING] User {user_id} attempted to check in with employment status '{employement_status}'")
            return error_response(message=EMPLOYMENT_STATUS_NOT_ACTIVE, status_code=400)

        try:
            # Check for existing check-in
            attendance = execute_query("""
                SELECT id, "checkInTime", "attendanceStatus"
                FROM attendance
                WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
            """, [user_id, today])

            day_of_week = checkin_time.strftime("%A")  # Get the day of the week (e.g., 'Monday')

            mapping_exists = execute_query(
                'SELECT 1 FROM "userShiftDayMapping" WHERE "userId" = %s LIMIT 1',
                [user_id],
                many=False
            )

            shift_result = execute_query("""
                SELECT s."shiftId", s."startTime", s."endTime"
                FROM "userShiftDayMapping" m
                INNER JOIN shifts s ON m."shiftId" = s."shiftId"
                WHERE m."userId" = %s AND m."dayOfWeek" = %s AND s."isDeleted" = 0
            """, [user_id, day_of_week])

            # If day-wise mapping exists for this user, day mapping is mandatory.
            if mapping_exists and not shift_result:
                return error_response(message=SHIFT_NOT_ASSIGNED, status_code=400)

            # Legacy fallback only when user has no day-wise mappings at all.
            if not mapping_exists and not shift_result:
                shift_result = execute_query("""
                    SELECT s."shiftId", s."startTime", s."endTime"
                    FROM "user" u
                    INNER JOIN shifts s ON u."shiftId" = s."shiftId"
                    WHERE u.id = %s AND u."isDeleted" = 0 AND s."isDeleted" = 0
                """, [user_id])

            if not shift_result or not shift_result[0]['shiftId']:
                return error_response(message=SHIFT_NOT_ASSIGNED, status_code=400)

            shift_id = shift_result[0]['shiftId']
            shift_start = shift_result[0]['startTime']
            shift_end = shift_result[0]['endTime']

            # Keep user.shiftId aligned with the shift used for this day/check-in.
            execute_query(
                'UPDATE "user" SET "shiftId" = %s WHERE id = %s',
                [shift_id, user_id],
                fetch=False
            )

            # Check if shift has ended
            if shift_end and checkin_time.time() > shift_end:
                return error_response(
                    message=CANNOT_CHECK_IN_AFTER_SHIFT_END,
                    status_code=400
                )

            shift_start_str = shift_start.strftime("%H:%M:%S") if shift_start else None
            shift_end_str = shift_end.strftime("%H:%M:%S") if shift_end else None


            if is_offline:
                attendance_status = 'Normal'

                # Check if attendance record exists
                if attendance and len(attendance) > 0:
                    attendance_id = attendance[0]['id']
                    existing_checkin_time = attendance[0].get('checkInTime')
                    
                    # If checkInTime is already set, data is already synced - return success without re-inserting
                    if existing_checkin_time:
                        logger.info(f"[CLOCKIN DEBUG] User {user_id} - Attendance ID {attendance_id} already has checkInTime: {existing_checkin_time}. Skipping duplicate offline sync.")
                        return success_response(
                            data={
                                'attendanceId': attendance_id,
                                'clockInTime': existing_checkin_time.strftime("%Y-%m-%d %H:%M:%S"),
                                'shiftId': shift_id,
                                'shiftStartTime': shift_start_str,
                                'shiftEndTime': shift_end_str,
                                'checkInDeviceId': checkin_device_id
                            },
                            message="Attendance already synced",
                            status_code=200
                        )
                    
                    # Existing attendance record with NULL checkInTime - UPDATE it
                    logger.info(f"[CLOCKIN DEBUG] User {user_id} - Updating attendance ID {attendance_id} for offline sync on {today}")

                    rows_updated = execute_query("""
                        UPDATE attendance
                        SET "checkInTime" = %s,
                            "assignedShiftAtCheckInId" = %s,
                            status = %s,
                            "checkInLatitude" = %s,
                            "checkInLongitude" = %s,
                            "updatedAt" = %s,
                            "checkInDeviceId" = %s,
                            "attendanceStatus" = %s
                        WHERE id = %s
                        AND "isDeleted" = 0
                    """, [
                        checkin_time,
                        shift_id,
                        'CHECKED_IN',
                        latitude,
                        longitude,
                        timezone.now(),
                        checkin_device_id,
                        attendance_status,
                        attendance_id
                    ])

                    if not rows_updated:
                        logger.error(f"[CLOCKIN ERROR] User {user_id} - Failed to update attendance ID {attendance_id}")
                        return error_response(
                            message="Offline sync failed. Could not update attendance record.",
                            status_code=400
                        )

                    logger.info(f"[CLOCKIN SUCCESS] User {user_id} - Offline sync: Updated attendance ID {attendance_id}, Time: {checkin_time.strftime('%Y-%m-%d %H:%M:%S')}, Shift: {shift_id}, Date: {today}")

                else:
                    # No attendance record exists - INSERT new record for offline sync
                    logger.info(f"[CLOCKIN DEBUG] User {user_id} - No attendance record found for {today}. Creating new record for offline sync.")

                    attendance_id = execute_query("""
                        INSERT INTO attendance (
                            "labourUserId", date, "checkInTime", "assignedShiftAtCheckInId",
                            status, "checkInLatitude", "checkInLongitude",
                            "createdAt", "updatedAt", "checkInDeviceId", "attendanceStatus", "isDeleted"
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, [
                        user_id, today, checkin_time, shift_id, 'CHECKED_IN', latitude, longitude,
                        timezone.now(), timezone.now(), checkin_device_id, attendance_status, 0
                    ])[0]['id']

                    logger.info(f"[CLOCKIN SUCCESS] User {user_id} - Offline sync: Created new attendance ID {attendance_id}, Time: {checkin_time.strftime('%Y-%m-%d %H:%M:%S')}, Shift: {shift_id}, Date: {today}")

                # Log activity for offline sync
                log_activity_raw(
                    request=request,
                    category='Attendance',
                    action='OfflineCheckIn',
                    performer=user,
                    target_employee_name=user.fullName,
                    target_shift_id=shift_id,
                    details={
                        'checkInTime': checkin_time_str,
                        'latitude': latitude,
                        'longitude': longitude,
                        'checkInDeviceId': checkin_device_id,
                        'isOfflineSync': True
                    }
                )

                return success_response(
                    data={
                        'attendanceId': attendance_id,
                        'clockInTime': checkin_time.strftime("%Y-%m-%d %H:%M:%S"),
                        'shiftId': shift_id,
                        'shiftStartTime': shift_start_str,
                        'shiftEndTime': shift_end_str,
                        'checkInDeviceId': checkin_device_id
                    },
                    message="Offline attendance synced successfully",
                    status_code=200
                )

            if attendance and attendance[0]['checkInTime']:
                return error_response(message=ALREADY_CHECKED_IN_TODAY, status_code=200)

            if attendance and attendance[0]['attendanceStatus'] == 'Absent':
                return error_response(message=YOU_ARE_MARKED_ABSENT_CONTACT_SUPERVISOR, status_code=400)

            
                


            # Create new attendance record
            attendance_id = execute_query("""
                INSERT INTO attendance (
                    "labourUserId", date, "checkInTime", "assignedShiftAtCheckInId",
                    status, "checkInLatitude", "checkInLongitude",
                    "createdAt", "updatedAt", "checkInDeviceId", "isDeleted"
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, [user_id, today, checkin_time, shift_id, 'CHECKED_IN', latitude, longitude,
                  timezone.now(), timezone.now(), checkin_device_id, 0])[0]['id']

            log_activity_raw(
                request=request,
                category='Attendance',
                action='CheckIn',
                performer=user,
                target_employee_name=user.fullName,
                target_shift_id=shift_id,
                details={
                    'checkInTime': checkin_time_str,
                    'latitude': latitude,
                    'longitude': longitude,
                    'checkInDeviceId': checkin_device_id
                }
            )

            # Store notification in database about successful check-in
            _send_notification(
                recipient_user_id=user_id,
                title="Check-In Successful",
                body=f"You checked in at {checkin_time.strftime('%I:%M %p')} on {checkin_time.strftime('%B %d, %Y')}.",
                notification_type="CHECK_IN",
                data_payload={
                    'attendanceId': str(attendance_id),
                    'checkInTime': checkin_time_str,
                    'type': 'CHECK_IN'
                },
                fcm_token=None,
                attendance_id=attendance_id,
                is_sync=True
            )

            return success_response(
                data={
                    'attendanceId': attendance_id,
                    'clockInTime': checkin_time.strftime("%Y-%m-%d %H:%M:%S"),
                    'shiftId': shift_id,
                    'shiftStartTime': shift_start_str,
                    'shiftEndTime': shift_end_str,
                    'checkInDeviceId': checkin_device_id
                },
                message=CHECKED_IN_SUCCESSFULLY,
                status_code=200
            )
        except Exception as e:
            print('error in checkin view',e)
            error_msg = f"Unexpected error during check-in: {str(e)}"
            logger.error(f"[CLOCKIN ERROR] User {user_id} - {error_msg}")
            logger.error(f"[CLOCKIN ERROR] Request data: {request.data}")
            logger.error(f"[CLOCKIN ERROR] Stack trace:\n{traceback.format_exc()}")
            return error_response(message="An error occurred during check-in. Please contact support.", status_code=500)

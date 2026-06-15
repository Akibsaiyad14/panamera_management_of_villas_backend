from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework import status as http_status
from django.conf import settings
import json

from ..utils import error_response, success_response, execute_query, log_activity_raw, _send_notification, format_hours_to_hhmm
from ..messages import (
    MISSING_REQUIRED_FIELDS,
    INVALID_FORMAT_FOR_INPUTS,
    EMERGENCY_CHECKIN_SUCCESSFUL,
    EMERGENCY_CHECKOUT_SUCCESSFUL,
    NO_SHIFT_ASSIGNED,
    SHIFT_NOT_COMPLETED_YET,
    NO_EMERGENCY_CHECKIN_FOUND,
    ALREADY_EMERGENCY_CHECKED_OUT
)


class EmergencyCheckinCheckoutStatus(APIView):
    """
    Provides the emergency check-in and check-out status for the authenticated user.
    """
    permission_classes = [IsAuthenticated]

    def _process_media_urls(self, request, media_json):
        """Convert relative media paths to full URLs."""
        if not media_json:
            return []
        try:
            paths = json.loads(media_json) if isinstance(media_json, str) else media_json
            return [request.build_absolute_uri(settings.MEDIA_URL + path) for path in paths]
        except (json.JSONDecodeError, TypeError):
            return []

    def get(self, request):
        user_id = request.data.get("userId") 
        today = timezone.now().date()

        attendance_result = execute_query("""
            SELECT id ,"emergencyCheckInTime", "emergencyCheckInLatitude", "emergencyCheckInLongitude", 
                   "emergencyCheckInDeviceId", "emergencyCheckOutTime", "emergencyCheckOutLatitude", 
                   "emergencyCheckOutLongitude", "emergencyCheckOutDeviceId", "emergencyHours", 
                   "emergencyReason", "emergencyCheckOutImages", "emergencyCheckOutAudio"
            FROM attendance
            WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
        """, [user_id, today], many=False)

        if not attendance_result:
            return error_response(
                message="No attendance record found for today.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        record = attendance_result[0]
        
        # Process media URLs to return full absolute URLs
        checkout_images = self._process_media_urls(request, record.get('emergencyCheckOutImages'))
        checkout_audio = self._process_media_urls(request, record.get('emergencyCheckOutAudio'))
        
        emergency_checkin_time = record.get('emergencyCheckInTime')
        emergency_checkout_time = record.get('emergencyCheckOutTime')

        status_data = {
            'attendanceId': record.get('id'),
            'checkInTime': emergency_checkin_time.strftime("%Y-%m-%d %H:%M:%S") if emergency_checkin_time else None,
            'checkOutTime': emergency_checkout_time.strftime("%Y-%m-%d %H:%M:%S") if emergency_checkout_time else None,
            'checkInLat': record.get('emergencyCheckInLatitude'),
            'checkInLong': record.get('emergencyCheckInLongitude'),
            'checkInDeviceId': record.get('emergencyCheckInDeviceId'),
            'checkOutLat': record.get('emergencyCheckOutLatitude'),
            'checkOutLong': record.get('emergencyCheckOutLongitude'),
            'checkOutDeviceId': record.get('emergencyCheckOutDeviceId'),
            'emergencyHours': record.get('emergencyHours'),
            'reason': record.get('emergencyReason'),
            'imageUrls': checkout_images,
            'audioUrls': checkout_audio,
            'status': 'COMPLETED' if emergency_checkin_time and emergency_checkout_time else (
                'CHECKED_IN' if emergency_checkin_time else 'NOT_CHECKED_IN'
            )
        }

        return success_response(
            data=status_data,
            message="Emergency check-in/check-out status retrieved successfully.",
            status_code=http_status.HTTP_200_OK
        )




class EmergencyCheckInView(APIView):
    """
    Handles emergency check-in for employees after their regular shift has completed.
    Emergency hours will be added to overtime.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        user_id = request.user.id

        try:
            checkin_time_str = request.data.get("dateTime")
            check_in_device_id = request.data.get("checkInDeviceId")
            latitude = float(request.data.get("latitude"))
            longitude = float(request.data.get("longitude"))
            is_offline = str(request.data.get("isOfflineData", "false")).lower() == "true"

            if not (checkin_time_str and check_in_device_id):
                return error_response(
                    message=MISSING_REQUIRED_FIELDS,
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            checkin_time = datetime.strptime(checkin_time_str, "%Y-%m-%d %H:%M:%S")
            today = checkin_time.date()

        except (ValueError, TypeError, AttributeError) as e:
            return error_response(
                message=INVALID_FORMAT_FOR_INPUTS + f" Error: {e}",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Check if there's a regular attendance record for today
        attendance_result = execute_query("""
            SELECT id, "checkInTime", "checkOutTime", "assignedShiftAtCheckInId",
                   "emergencyCheckInTime", "emergencyCheckOutTime"
            FROM attendance
            WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
        """, [user_id, today], many=False)

        # If no regular attendance found, user must complete regular shift first
        if not attendance_result:
            return error_response(
                message="No regular attendance found for today. Please complete your regular shift first.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        record = attendance_result[0]

        # Check if regular shift is completed (must have checkOutTime)
        if not record.get('checkOutTime'):
            return error_response(
                message=SHIFT_NOT_COMPLETED_YET,
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Handle offline sync
        if is_offline:
            existing_emergency_checkin = record.get('emergencyCheckInTime')
            
            # If emergency check-in already exists, skip (already synced)
            if existing_emergency_checkin:
                return success_response(
                    data={
                        'attendanceId': record['id'],
                        'emergencyCheckInTime': existing_emergency_checkin.strftime("%Y-%m-%d %H:%M:%S"),
                        'regularCheckOutTime': record.get('checkOutTime').strftime("%Y-%m-%d %H:%M:%S") if record.get('checkOutTime') else None
                    },
                    message="Emergency check-in already synced",
                    status_code=http_status.HTTP_200_OK
                )
        
        # Check if already emergency checked in (for online requests)
        if not is_offline and record.get('emergencyCheckInTime') and not record.get('emergencyCheckOutTime'):
            return error_response(
                message="Already emergency checked in. Please check out first.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Validate that emergency check-in is after regular checkout
        regular_checkout_time = record['checkOutTime']
        if checkin_time <= regular_checkout_time:
            return error_response(
                message="Emergency check-in must be after regular check-out time.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Get shift details to verify shift has ended
        shift_id = record.get('assignedShiftAtCheckInId')
        if not shift_id:
            return error_response(
                message=NO_SHIFT_ASSIGNED,
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        shift_result = execute_query("""
            SELECT "startTime", "endTime" FROM shifts WHERE "shiftId" = %s AND "isDeleted" = 0
        """, [shift_id], many=False)

        if not shift_result:
            return error_response(
                message="Shift details not found.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        shift_record = shift_result[0]
        shift_end_time = shift_record['endTime']
        shift_end_dt = datetime.combine(today, shift_end_time)

        # Handle overnight shifts
        shift_start_time = shift_record['startTime']
        shift_start_dt = datetime.combine(today, shift_start_time)
        if shift_end_dt <= shift_start_dt:
            shift_end_dt += timedelta(days=1)

        # Emergency check-in should be after shift end time
        if checkin_time < shift_end_dt:
            return error_response(
                message=f"Emergency check-in is only allowed after shift end time ({shift_end_time.strftime('%H:%M')}).",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Update attendance record with emergency check-in
        execute_query("""
            UPDATE attendance
            SET "emergencyCheckInTime" = %s,
                "emergencyCheckInLatitude" = %s,
                "emergencyCheckInLongitude" = %s,
                "emergencyCheckInDeviceId" = %s,
                "updatedAt" = NOW()
            WHERE id = %s
        """, [
            checkin_time, latitude, longitude, check_in_device_id, record['id']
        ])

        log_activity_raw(
            request=request,
            category='Attendance',
            action='OfflineEmergencyCheckIn' if is_offline else 'EmergencyCheckIn',
            performer=user,
            target_employee_name=getattr(user, 'fullName', None),
            target_shift_id=shift_id,
            details={
                'emergencyCheckInTime': checkin_time_str,
                'latitude': latitude,
                'longitude': longitude,
                'attendanceId': record['id'],
                'isOfflineSync': is_offline
            }
        )

        # Store notification in database about emergency check-in
        _send_notification(
            recipient_user_id=user_id,
            title="Emergency Check-In Successful",
            body=f"You completed emergency check-in at {checkin_time.strftime('%I:%M %p')} on {checkin_time.strftime('%B %d, %Y')}.",
            notification_type="EMERGENCY_CHECK_IN",
            data_payload={
                'attendanceId': str(record['id']),
                'emergencyCheckInTime': checkin_time_str,
                'type': 'EMERGENCY_CHECK_IN'
            },
            fcm_token=None,
            attendance_id=record['id'],
            is_sync=True
        )

        success_message = "Emergency check-in synced successfully" if is_offline else EMERGENCY_CHECKIN_SUCCESSFUL
        
        return success_response(
            data={
                'attendanceId': record['id'],
                'emergencyCheckInTime': checkin_time.strftime("%Y-%m-%d %H:%M:%S"),
                'regularCheckOutTime': regular_checkout_time.strftime("%Y-%m-%d %H:%M:%S")
            },
            message=success_message,
            status_code=http_status.HTTP_200_OK
        )


class EmergencyCheckOutView(APIView):
    """
    Handles emergency check-out for employees.
    Calculates emergency hours and adds them to the day's overtime.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        user_id = request.user.id

        try:
            checkout_time_str = request.data.get("dateTime")
            check_out_device_id = request.data.get("checkOutDeviceId")
            latitude = float(request.data.get("latitude"))
            longitude = float(request.data.get("longitude"))
            is_offline = str(request.data.get("isOfflineData", "false")).lower() == "true"

            if not (checkout_time_str and check_out_device_id):
                return error_response(
                    message=MISSING_REQUIRED_FIELDS,
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            checkout_time = datetime.strptime(checkout_time_str, "%Y-%m-%d %H:%M:%S")

        except (ValueError, TypeError, AttributeError) as e:
            return error_response(
                message=INVALID_FORMAT_FOR_INPUTS + f" Error: {e}",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Fetch attendance record - look for most recent emergency check-in without checkout
        # This handles overnight scenarios where checkout is on the next day
        record_result = execute_query("""
            SELECT id, date, "checkInTime", "checkOutTime", "emergencyCheckInTime",
                   "emergencyCheckOutTime", "overtimeHours", "calculatedRegularHours",
                   "assignedShiftAtCheckInId"
            FROM attendance
            WHERE "labourUserId" = %s 
                AND "emergencyCheckInTime" IS NOT NULL 
                AND "emergencyCheckOutTime" IS NULL
                AND "isDeleted" = 0
            ORDER BY "emergencyCheckInTime" DESC
            LIMIT 1
        """, [user_id], many=False)

        if not record_result:
            return error_response(
                message="No attendance record found for today.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        record = record_result[0]

        # Validate emergency check-in exists
        if not record.get('emergencyCheckInTime'):
            return error_response(
                message=NO_EMERGENCY_CHECKIN_FOUND,
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Handle offline sync
        if is_offline:
            existing_emergency_checkout = record.get('emergencyCheckOutTime')
            
            # If emergency check-out already exists, skip (already synced)
            if existing_emergency_checkout:
                return success_response(
                    data={
                        'attendanceId': record['id'],
                        'emergencyCheckInTime': record.get('emergencyCheckInTime').strftime("%Y-%m-%d %H:%M:%S"),
                        'emergencyCheckOutTime': existing_emergency_checkout.strftime("%Y-%m-%d %H:%M:%S"),
                        'emergencyHours': record.get('emergencyHours'),
                        'totalOvertimeHours': record.get('overtimeHours'),
                        'status': 'COMPLETED'
                    },
                    message="Emergency check-out already synced",
                    status_code=http_status.HTTP_200_OK
                )
        
        # Check if already emergency checked out (for online requests)
        if not is_offline and record.get('emergencyCheckOutTime'):
            return error_response(
                message=ALREADY_EMERGENCY_CHECKED_OUT,
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        emergency_checkin_time = record['emergencyCheckInTime']

        # Validate checkout is after emergency checkin
        if checkout_time <= emergency_checkin_time:
            return error_response(
                message="Emergency check-out must be after emergency check-in time.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Calculate emergency hours
        emergency_duration = checkout_time - emergency_checkin_time
        emergency_hours = round(emergency_duration.total_seconds() / 3600, 2)

        # Get current overtime hours and add emergency hours
        current_overtime = float(record.get('overtimeHours') or 0)
        total_overtime = round(current_overtime + emergency_hours, 2)

        # Update overtime status
        overtime_status = 1 if total_overtime > 0 else 0

        # Update attendance record
        execute_query("""
            UPDATE attendance
            SET "emergencyCheckOutTime" = %s,
                "emergencyCheckOutLatitude" = %s,
                "emergencyCheckOutLongitude" = %s,
                "emergencyCheckOutDeviceId" = %s,
                "emergencyHours" = %s,
                "overtimeHours" = %s,
                "overtimeStatus" = %s,
                "updatedAt" = NOW()
            WHERE id = %s
        """, [
            checkout_time, latitude, longitude, check_out_device_id,
            emergency_hours, total_overtime, overtime_status, record['id']
        ])

        # Create or update overtime request
        overtime_request_id = None
        if total_overtime > 0:
            # Check if overtime request already exists
            existing_ot = execute_query("""
                SELECT "overtimeId", "actualCheckoutTime"
                FROM "overtimeRequests"
                WHERE "attendanceRecordId" = %s
            """, [record['id']], many=False)

            if existing_ot:
                # Update existing overtime request
                overtime_request_id = existing_ot[0]['overtimeId']
                execute_query("""
                    UPDATE "overtimeRequests"
                    SET "actualCheckoutTime" = %s,
                        "updatedAt" = NOW()
                    WHERE "overtimeId" = %s
                """, [checkout_time, overtime_request_id])
            else:
                # Create new overtime request
                ot_result = execute_query("""
                    INSERT INTO "overtimeRequests" ("attendanceRecordId", "actualCheckoutTime", "createdAt", "updatedAt")
                    VALUES (%s, %s, NOW(), NOW()) RETURNING "overtimeId"
                """, [record['id'], checkout_time], fetch=True)
                if ot_result:
                    overtime_request_id = ot_result[0]['overtimeId']

        log_activity_raw(
            request=request,
            category='Attendance',
            action='OfflineEmergencyCheckOut' if is_offline else 'EmergencyCheckOut',
            performer=user,
            target_employee_name=getattr(user, 'fullName', None),
            target_shift_id=record.get('assignedShiftAtCheckInId'),
            details={
                'emergencyCheckOutTime': checkout_time_str,
                'latitude': latitude,
                'longitude': longitude,
                'emergencyHours': emergency_hours,
                'totalOvertimeHours': total_overtime,
                'overtimeRequestId': overtime_request_id,
                'attendanceId': record['id'],
                'isOfflineSync': is_offline
            }
        )

        formatted_emergency_hours = format_hours_to_hhmm(emergency_hours)
        formatted_total_overtime = format_hours_to_hhmm(total_overtime)

        # Store notification in database about emergency check-out
        _send_notification(
            recipient_user_id=user_id,
            title="Emergency Check-Out Successful",
            body=f"You completed emergency check-out at {checkout_time.strftime('%I:%M %p')}. Emergency hours: {formatted_emergency_hours}, Total overtime: {formatted_total_overtime}.",
            notification_type="EMERGENCY_CHECK_OUT",
            data_payload={
                'attendanceId': str(record['id']),
                'emergencyCheckOutTime': checkout_time_str,
                'emergencyHours': formatted_emergency_hours,
                'totalOvertimeHours': formatted_total_overtime,
                'type': 'EMERGENCY_CHECK_OUT'
            },
            fcm_token=None,
            attendance_id=record['id'],
            is_sync=True
        )

        # Notify Team Leader and Supervisor about overtime approval required for emergency work
        try:
            employee_name = getattr(user, 'fullName', 'Employee')
            employee_emp_id = getattr(user, 'employeeId', None)
            attendance_date = record.get('date')
            date_formatted = attendance_date.strftime('%B %d, %Y') if attendance_date else 'today'
            
            # Get user's supervisor and all team leaders under the same supervisor
            supervisor_query = """
                SELECT u."reportingToId", u."teamLeaderId",
                       sup."fcmToken" as sup_fcm_token,
                       sup."fullName" as sup_name
                FROM "user" u
                LEFT JOIN "user" sup ON u."reportingToId" = sup.id AND COALESCE(sup."isDeleted", '0') = '0'
                WHERE u.id = %s AND COALESCE(u."isDeleted", '0') = '0'
            """
            supervisor_result = execute_query(supervisor_query, [user_id], many=False)
            
            if supervisor_result:
                supervisor_id = supervisor_result[0].get('reportingToId')
                direct_team_leader_id = supervisor_result[0].get('teamLeaderId')
                sup_fcm_token = supervisor_result[0].get('sup_fcm_token')
                
                notification_title = "Emergency Work Overtime Approval Required"
                notification_body = f"{employee_name} completed emergency work on {date_formatted}. Emergency hours: {formatted_emergency_hours}. Overtime approval required."
                notification_data = {
                    'employeeId': str(user_id),
                    'employeeName': employee_name,
                    'employeeEmpId': employee_emp_id,
                    'attendanceId': str(record['id']),
                    'emergencyHours': formatted_emergency_hours,
                    'totalOvertimeHours': formatted_total_overtime,
                    'date': attendance_date.strftime('%Y-%m-%d') if attendance_date else None,
                    'overtimeRequestId': str(overtime_request_id) if overtime_request_id else None,
                    'type': 'EMERGENCY_OVERTIME_APPROVAL_REQUIRED'
                }
                
                # Get all team leaders under the same supervisor (including directly assigned and peer team leaders)
                team_leaders_to_notify = set()  # Use set to avoid duplicates
                
                if supervisor_id:
                    # Find all team leaders reporting to the same supervisor
                    team_leaders_query = """
                        SELECT DISTINCT u.id, u."fcmToken", u."fullName"
                        FROM "user" u
                        JOIN "userrole" ur ON u."roleId" = ur."roleId"
                        WHERE u."reportingToId" = %s 
                        AND ur."isTeamLeader" = true
                        AND COALESCE(u."isDeleted", '0') = '0'
                        AND u.id != %s
                    """
                    team_leaders_result = execute_query(team_leaders_query, [supervisor_id, user_id], many=True)
                    
                    if team_leaders_result:
                        for tl in team_leaders_result:
                            tl_id = tl.get('id')
                            if tl_id and tl_id != user_id:
                                team_leaders_to_notify.add((tl_id, tl.get('fcmToken')))
                
                # Also add directly assigned team leader if exists and not already in the set
                if direct_team_leader_id and direct_team_leader_id != user_id:
                    # Get FCM token for direct team leader
                    direct_tl_query = """
                        SELECT "fcmToken" FROM "user" 
                        WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
                    """
                    direct_tl_result = execute_query(direct_tl_query, [direct_team_leader_id], many=False)
                    if direct_tl_result:
                        tl_fcm_token = direct_tl_result[0].get('fcmToken')
                        team_leaders_to_notify.add((direct_team_leader_id, tl_fcm_token))
                
                # Send notifications to all team leaders
                notified_count = 0
                for tl_id, tl_fcm_token in team_leaders_to_notify:
                    _send_notification(
                        recipient_user_id=tl_id,
                        title=notification_title,
                        body=notification_body,
                        notification_type="EMERGENCY_OVERTIME_APPROVAL",
                        data_payload=notification_data,
                        fcm_token=tl_fcm_token,
                        attendance_id=record['id']
                    )
                    notified_count += 1
                
                if notified_count > 0:
                    print(f"Sent emergency overtime notifications to {notified_count} team leader(s)")
                else:
                    print(f"No team leaders found to notify for user {user_id}")
                
                # Notify Supervisor (only if different from employee)
                if supervisor_id and supervisor_id != user_id:
                    _send_notification(
                        recipient_user_id=supervisor_id,
                        title=notification_title,
                        body=notification_body,
                        notification_type="EMERGENCY_OVERTIME_APPROVAL",
                        data_payload=notification_data,
                        fcm_token=sup_fcm_token,
                        attendance_id=record['id']
                    )
                    print(f"Sent emergency overtime notification to supervisor")
                elif supervisor_id == user_id:
                    print(f"Skipped Supervisor notification (employee is their own supervisor)")
        
        except Exception as notify_error:
            # Log error but don't fail the checkout
            print(f"Failed to send notifications to team leader/supervisor: {str(notify_error)}")

        success_message = "Emergency check-out synced successfully" if is_offline else EMERGENCY_CHECKOUT_SUCCESSFUL
        
        return success_response(
            data={
                'attendanceId': record['id'],
                'emergencyCheckInTime': emergency_checkin_time.strftime("%Y-%m-%d %H:%M:%S"),
                'emergencyCheckOutTime': checkout_time.strftime("%Y-%m-%d %H:%M:%S"),
                'emergencyHours': formatted_emergency_hours,
                'totalOvertimeHours': formatted_total_overtime,
                'overtimeRequestId': overtime_request_id,
                'status': 'COMPLETED'
            },
            message=success_message,
            status_code=http_status.HTTP_200_OK
        )

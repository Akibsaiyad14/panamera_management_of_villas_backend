from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, _calculate_attendance_details, log_activity_raw
from datetime import datetime
from django.utils import timezone
from django.db import transaction


class AddAttendanceView(APIView):
    permission_classes = [IsAuthenticated]
    """
    API endpoint to add or update attendance records for users.
    """

    @transaction.atomic
    def post(self, request):
        try:
            user_id = request.data.get("userId")
            checkin_time_str = request.data.get("checkInTime")
            checkout_time_str = request.data.get("checkOutTime")

            employement_status_query = execute_query('SELECT "employmentStatus" FROM "user" WHERE id = %s', [user_id])

            if not user_id:
                return error_response(message=USER_ID_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)
            if not checkin_time_str:
                return error_response(message=CHECKIN_TIME_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

            if employement_status_query and employement_status_query[0].get('employmentStatus') != 'Active':
                return error_response(message=EMPLOYEE_NOT_ACTIVE, status_code=status.HTTP_400_BAD_REQUEST)

            checkin_time = datetime.strptime(checkin_time_str, "%Y-%m-%d %H:%M:%S")
            checkout_time = datetime.strptime(checkout_time_str, "%Y-%m-%d %H:%M:%S") if checkout_time_str else None
            attendance_date = checkin_time.date()

            if checkout_time and checkout_time < checkin_time:
                return error_response(message=CHECKOUT_BEFORE_CHECKIN, status_code=status.HTTP_400_BAD_REQUEST)

            existing_record = execute_query(
                'SELECT "id" FROM attendance WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0',
                [user_id, attendance_date]
            )
            if existing_record:
                return error_response(message=ATTENDANCE_ALREADY_EXISTS_FOR_DATE, status_code=status.HTTP_409_CONFLICT)

            day_of_week = checkin_time.strftime("%A")  # Get the day of the week (e.g., 'Monday')

            mapping_exists = execute_query(
                'SELECT 1 FROM "userShiftDayMapping" WHERE "userId" = %s LIMIT 1',
                [user_id],
                many=False
            )

            user_shift_assignment = execute_query("""
                SELECT m."shiftId"
                FROM "userShiftDayMapping" m
                INNER JOIN shifts s ON m."shiftId" = s."shiftId"
                WHERE m."userId" = %s AND m."dayOfWeek" = %s AND s."isDeleted" = 0
            """, [user_id, day_of_week])

            # If day-wise mapping exists for user, day mapping is mandatory.
            if mapping_exists and not user_shift_assignment:
                return error_response(
                    message=USER_NO_SHIFT_ASSIGNED,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Legacy fallback only when user has no day-wise mappings at all.
            if not mapping_exists and not user_shift_assignment:
                user_shift_assignment = execute_query("""
                    SELECT u."shiftId"
                    FROM "user" u
                    INNER JOIN shifts s ON u."shiftId" = s."shiftId"
                    WHERE u.id = %s AND u."isDeleted" = 0 AND s."isDeleted" = 0
                """, [user_id])


            user_assigned_shift_id = None
            if isinstance(user_shift_assignment, list) and user_shift_assignment:
                user_assigned_shift_id = user_shift_assignment[0].get('shiftId')

            # --- NEW: Check if user has ANY shift assigned ---
            if user_assigned_shift_id is None:
                return error_response(
                    message=USER_NO_SHIFT_ASSIGNED,
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            # --- END NEW CHECK ---

            # If user has a shiftId assigned but we can't find the shift details
            shift_record = None
            shift_id_for_insert = None

            if user_assigned_shift_id: # This block will now always execute if we reach here
                shift_details_query_result = execute_query("""
                    SELECT "shiftId", "startTime", "endTime"
                    FROM shifts
                    WHERE "shiftId" = %s AND "isDeleted" = 0
                """, [user_assigned_shift_id])

                if shift_details_query_result:
                    shift_record = shift_details_query_result[0]
                    shift_id_for_insert = shift_record.get('shiftId')
                else:
                    return error_response(
                        message=f"{SHIFT_NOT_FOUND_FOR_USER} (User {user_id} assigned to Shift ID {user_assigned_shift_id} but shift details not found or deleted)",
                        status_code=status.HTTP_404_NOT_FOUND
                    )

            # Keep user.shiftId aligned with the shift used for this attendance date.
            execute_query(
                'UPDATE "user" SET "shiftId" = %s WHERE id = %s',
                [shift_id_for_insert, user_id],
                fetch=False
            )

            # At this point, shift_record and shift_id_for_insert are guaranteed to be populated
            # if the user has an assigned, valid shift.

            calc_details = _calculate_attendance_details(checkin_time, checkout_time, shift_record)

            overtime_hours = calc_details['overtime_hours']
            regular_hours = calc_details['regular_hours']
            attendance_status = calc_details['attendance_status']
            overtime_status = 1 if overtime_hours > 0 else 0
            status_value = 'COMPLETED' if checkout_time else 'CHECKED_IN'

            attendance_id_result = execute_query("""
                INSERT INTO attendance (
                    "labourUserId", date, "checkInTime", "checkOutTime", "assignedShiftAtCheckInId",
                    status, "calculatedRegularHours", "overtimeHours", "attendanceStatus", "overtimeStatus"
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, [
                user_id, attendance_date, checkin_time, checkout_time, shift_id_for_insert,
                status_value, regular_hours, overtime_hours, attendance_status, overtime_status
            ], fetch=True)

            if not (isinstance(attendance_id_result, list) and attendance_id_result and isinstance(attendance_id_result[0], dict) and attendance_id_result[0].get('id')):
                return error_response(
                    message=ERROR_RECORDING_ATTENDANCE,
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            attendance_id = attendance_id_result[0]['id']

            if overtime_hours > 0:
                execute_query("""
                    INSERT INTO "overtimeRequests" ("attendanceRecordId", "actualCheckoutTime")
                    VALUES (%s, %s)
                """, [attendance_id, checkout_time])

            if attendance_status == 'Halfday':
                execute_query("""
                    UPDATE "attendance"
                        SET "earlyReasonStatus" = 1,
                            "earlyReason" = ''
                        WHERE id = %s

                """, [attendance_id])

            user_info_result = execute_query(
                'SELECT "fullName", "employeeId" FROM "user" WHERE id = %s', [user_id]
            )
            if not (isinstance(user_info_result, list) and user_info_result and isinstance(user_info_result[0], dict)):
                return error_response(message=EMPLOYEE_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)
            user_info = user_info_result[0]

            log_activity_raw(
                request=request,
                category='Attendance',
                action='Add',
                performer=request.user,
                target_employee_name=user_info['fullName'],
                target_shift_id=shift_id_for_insert,
                details={
                    'addedForUser': user_info['fullName'],
                    'checkInTime': str(checkin_time),
                    'checkOutTime': str(checkout_time) if checkout_time else None,
                    'regularHours': calc_details['regular_hours'],
                    'overtimeHours': calc_details['overtime_hours']
                }
            )

            return success_response(
                data={
                    'attendanceId': attendance_id,
                    'fullName': user_info['fullName'],
                    'employeeId': user_info['employeeId']
                },
                message=ATTENDANCE_ENTRY_RECORDED_SUCCESSFULLY,
                status_code=status.HTTP_201_CREATED
            )

        except ValueError as e:
            return error_response(message=f"{INVALID_FORMAT_FOR_INPUTS} {str(e)}", status_code=status.HTTP_400_BAD_REQUEST)
        except TypeError as e:
            return error_response(message=f"{ERROR_RECORDING_ATTENDANCE} {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return error_response(message=f"{ERROR_RECORDING_ATTENDANCE} {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @transaction.atomic
    def put(self, request):
        user = request.user
        user_id = user.id
        user_info = execute_query('SELECT "employeeId", "roleId" FROM "user" WHERE id = %s', [user_id])
        if not (isinstance(user_info, list) and user_info and isinstance(user_info[0], dict)):
            return error_response(message=USER_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)
        user_info = user_info[0]
        user_role_id = user_info.get('roleId')
        
        attendance_id = request.data.get("attendanceId")
        overtime_hours_update = request.data.get("overtimeHours")

        if not attendance_id:
            return error_response(message=ATTENDANCE_ID_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)
        
        # Check if user is trying to update overtime hours
        if overtime_hours_update is not None:
            # Only allow roleId 1, 2, or 3 to update overtime hours
            allowed_roles = [1, 2, 3]  # Superadmin, Office Admin, Office Admin 2
            if user_role_id not in allowed_roles:
                return error_response(
                    message=NO_PERMISSION_UPDATE_OVERTIME_HOURS,
                    status_code=status.HTTP_403_FORBIDDEN
                )

            result = execute_query("""
                    UPDATE attendance
                    SET "overtimeHours" = %s, "updatedAt" = NOW()
                    WHERE id = %s AND "isDeleted" = 0
                    RETURNING id, "labourUserId"
                """, [overtime_hours_update, attendance_id], fetch=True
                )
            
            if not result:
                return error_response(
                    message=FAILED_UPDATE_OVERTIME_HOURS,
                    status_code=status.HTTP_404_NOT_FOUND
                )
            
            # If only overtime hours is being updated (no checkInTime/checkOutTime), return success
            if not request.data.get("checkInTime") and not request.data.get("checkOutTime"):
                return success_response(
                    data={"attendanceId": attendance_id, "overtimeHours": overtime_hours_update},
                    message=OVERTIME_HOURS_UPDATED_SUCCESSFULLY,
                    status_code=status.HTTP_200_OK
                )
            # If checkInTime/checkOutTime provided along with overtimeHours, continue to update all fields
            # but preserve the manual overtime hours value


        try:
            record_query = execute_query("""
                SELECT id, "labourUserId", "checkInTime", "checkOutTime", "assignedShiftAtCheckInId", "date", "overtimeStatus"
                FROM attendance
                WHERE id = %s AND "isDeleted" = 0
            """, [attendance_id])

            if not (isinstance(record_query, list) and record_query and isinstance(record_query[0], dict)):
                return error_response(message=RECORD_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

            record = record_query[0]

            target_user_id = record.get('labourUserId')
            user_info = execute_query('SELECT "employeeId", "fullName" FROM "user" WHERE id = %s', [target_user_id])
            if not user_info:
                # This case is unlikely if the attendance record exists, but it's good practice
                return error_response(message=EMPLOYEE_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)
            user_info = user_info[0]


            checkin_time_str = request.data.get("checkInTime")
            checkout_time_str = request.data.get("checkOutTime")

            new_checkin_time = datetime.strptime(checkin_time_str, "%Y-%m-%d %H:%M:%S") if checkin_time_str else record.get('checkInTime')
            new_checkout_time = datetime.strptime(checkout_time_str, "%Y-%m-%d %H:%M:%S") if checkout_time_str else record.get('checkOutTime')

            if not new_checkin_time:
                return error_response(message=CHECKIN_TIME_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)
            if new_checkout_time and new_checkout_time < new_checkin_time:
                return error_response(message=CHECKOUT_BEFORE_CHECKIN, status_code=status.HTTP_400_BAD_REQUEST)

            # --- START OF SHIFT RESOLUTION LOGIC ---
            # Always resolve the correct shift for the attendance date's day-of-week
            shift_id = None
            shift_record = None
            warning_message = None

            if target_user_id:
                attendance_date = record.get('date')
                if attendance_date:
                    if isinstance(attendance_date, datetime):
                        day_of_week = attendance_date.strftime('%A')
                    else:
                        day_of_week = datetime.combine(attendance_date, datetime.min.time()).strftime('%A')

                    mapping_exists = execute_query(
                        'SELECT 1 FROM "userShiftDayMapping" WHERE "userId" = %s LIMIT 1',
                        [target_user_id],
                        many=False
                    )

                    user_shift_query = execute_query("""
                        SELECT m."shiftId"
                        FROM "userShiftDayMapping" m
                        INNER JOIN shifts s ON m."shiftId" = s."shiftId"
                        WHERE m."userId" = %s AND m."dayOfWeek" = %s AND s."isDeleted" = 0
                    """, [target_user_id, day_of_week])

                    if user_shift_query and user_shift_query[0].get('shiftId'):
                        shift_id = user_shift_query[0].get('shiftId')
                    else:
                        # If mapping records exist for user, day mapping is mandatory.
                        if mapping_exists:
                            return error_response(
                                message=USER_NO_SHIFT_ASSIGNED,
                                status_code=status.HTTP_400_BAD_REQUEST
                            )
                        else:
                            # Legacy fallback to user.shiftId, then fallback to attendance stored shift
                            legacy_shift_query = execute_query(
                                '''SELECT u."shiftId"
                                   FROM "user" u
                                   INNER JOIN shifts s ON u."shiftId" = s."shiftId"
                                   WHERE u.id = %s AND u."isDeleted" = 0 AND s."isDeleted" = 0''',
                                [target_user_id]
                            )
                            if legacy_shift_query and legacy_shift_query[0].get('shiftId'):
                                shift_id = legacy_shift_query[0].get('shiftId')
                            else:
                                shift_id = record.get('assignedShiftAtCheckInId')
                        if shift_id:
                            warning_message = (
                                f"No shift mapping found for {day_of_week}. "
                                "Using legacy/default shift assignment."
                            )
            
            if not shift_id:
                shift_id = record.get('assignedShiftAtCheckInId')

            # Keep user.shiftId aligned with the resolved shift used in update flow.
            if shift_id:
                execute_query(
                    'UPDATE "user" SET "shiftId" = %s WHERE id = %s',
                    [shift_id, target_user_id],
                    fetch=False
                )
            # print("shift_id", shift_id)
            # Now, proceed if we have a shift_id from either the record or the fallback
            if shift_id:
                # print(shift_id)
                shift_result = execute_query(
                    'SELECT "startTime", "endTime" FROM shifts WHERE "shiftId" = %s AND "isDeleted" = 0', [shift_id]
                )
                if shift_result:
                    print(shift_result)
                    shift_record = shift_result[0]
                else:
                    # Handle case where shift ID exists but shift is deleted/not found
                    return error_response(message=SHIFT_NOT_FOUND_OR_DELETED.format(shift_id=shift_id), status_code=status.HTTP_404_NOT_FOUND)
            # If after all this, shift_record is still None, calculations will proceed without shift context
            # which is the original behavior, but now only as a last resort.

            # --- END OF SHIFT RESOLUTION LOGIC ---

            calc_details = _calculate_attendance_details(new_checkin_time, new_checkout_time, shift_record)
            # print("calc_details", calc_details)

            

            # ... (rest of the calculation and update logic is the same)
            # Use manually provided overtime hours if available, otherwise use calculated value
            if overtime_hours_update is not None:
                overtime_hours = overtime_hours_update
                # Preserve existing overtimeStatus when overtime is manually provided
                overtime_status_val = record.get('overtimeStatus', 1)
            else:
                overtime_hours = calc_details['overtime_hours']
                # Calculate overtimeStatus only when overtime is calculated
                overtime_status_val = 1 if overtime_hours > 0 else 0
            
            regular_hours = calc_details['regular_hours']
            attendance_status = calc_details['attendance_status']
            final_status = 'COMPLETED' if new_checkout_time else 'CHECKED_IN'

            execute_query("""
                UPDATE attendance
                SET "checkInTime" = %s, "checkOutTime" = %s, "calculatedRegularHours" = %s,
                    "overtimeHours" = %s, "attendanceStatus" = %s, "overtimeStatus" = %s,
                    "status" = %s, "assignedShiftAtCheckInId" = %s, "updatedAt" = NOW()
                WHERE id = %s
            """, [
                new_checkin_time, new_checkout_time, regular_hours, overtime_hours,
                attendance_status, overtime_status_val, final_status, shift_id, attendance_id
            ])

            # Update earlyReasonStatus based on attendance status
            if attendance_status == 'Halfday':
                execute_query("""
                    UPDATE "attendance"
                        SET "earlyReasonStatus" = 1,
                            "earlyReason" = ''
                        WHERE id = %s
                """, [attendance_id])
            else:
                # Reset earlyReasonStatus when status is not Halfday
                execute_query("""
                    UPDATE "attendance"
                        SET "earlyReasonStatus" = 0,
                            "earlyReason" = NULL
                        WHERE id = %s
                """, [attendance_id])
            # ... (delete/insert overtime requests, logging, etc.)
            
            # In your log activity, you now correctly use the target employee's ID
            log_activity_raw(
                request=request,
                category='Attendance',
                action='Update',
                performer=request.user,
                target_employee_name=user_info['fullName'],
                target_shift_id=shift_id, # Use the determined shift_id
                details={
                    'updatedAttendanceId': attendance_id,
                    'newCheckInTime': str(new_checkin_time),
                    'newCheckOutTime': str(new_checkout_time) if new_checkout_time else None,
                    'newOvertimeHours': calc_details['overtime_hours']
                }
            )

            response_data = {
                'attendanceId': attendance_id,
                'attendanceStatus': attendance_status,
            }
            if warning_message:
                response_data['warning'] = warning_message

            return success_response(
                data=response_data,
                message=ATTENDANCE_ENTRY_UPDATED_SUCCESSFULLY,
                status_code=status.HTTP_200_OK
            )

        except (ValueError, TypeError) as e:
            return error_response(f"{INVALID_DATETIME_FORMAT}: {str(e)}", status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return error_response(f"{ERROR_UPDATING_ATTENDANCE}: {str(e)}", status.HTTP_500_INTERNAL_SERVER_ERROR)



    def delete(self, request):
        try:
            user = request.user
            user_ids = request.data.get("userIds")

            if not user_ids or not isinstance(user_ids, list):
                return error_response(
                    message=LIST_OF_USERIDS_REQUIRED,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Check if records exist
            placeholders = ','.join(['%s'] * len(user_ids))
            check_query = f'''
                SELECT a."id", a."labourUserId", u."fullName"
                FROM "attendance" a
                JOIN "user" u ON a."labourUserId" = u."id"
                WHERE a."id" IN ({placeholders}) AND a."isDeleted" = 0
            '''
            existing_records = execute_query(check_query, user_ids, many=True)

            if existing_records is False:
                return error_response(
                    message=ERROR_CHECKING_ATTENDANCE_RECORDS,
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            if not existing_records:
                return error_response(
                    data=None,
                    message=NO_MATCHING_ATTENDANCE_RECORDS_FOR_DELETION,
                    status_code=status.HTTP_404_NOT_FOUND
                )

            existing_ids = [str(record['id']) for record in existing_records]

            # Soft delete records
            delete_query = f'''
                UPDATE "attendance"
                SET "isDeleted" = 1, "updatedAt" = NOW()
                WHERE "id" IN ({placeholders})
            '''
            delete_result = execute_query(delete_query, user_ids, fetch=False, commit=True)

            if delete_result is False:
                return error_response(
                    message=ERROR_EXECUTING_DELETE_OPERATION,
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Prepare details with fullName
            deleted_attendance_info = [
                {
                    "attendanceId": record["id"],
                    "labourUserId": record["labourUserId"],
                    "fullName": record["fullName"]
                }
                for record in existing_records
            ]

            log_activity_raw(
                request=request,
                category='Attendance',
                action='Delete',
                performer=user,
                target_employee_name=user.fullName,
                details={
                    "deletedAttendance": deleted_attendance_info,
                    "count": len(deleted_attendance_info)
                }
            )

            return success_response(
                message=ATTENDANCE_RECORDS_DELETED_SUCCESSFULLY,
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(
                message=f"{ERROR_DELETING_ATTENDANCE_RECORDS} {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
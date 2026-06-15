from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from api.utils import success_response, error_response, execute_query, log_activity_raw, _calculate_attendance_details, save_overtime_files
from api.constants import *
import json


class UpdateOvertimeStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_user_role_order(self, user_id):
        """Get the caller's roleOrderId and isTeamLeader flag."""
        result = execute_query(
            """SELECT ur."groupNumber", ur."isTeamLeader"
               FROM "user" u
               JOIN "userrole" ur ON u."roleId" = ur."roleId"
               WHERE u.id = %s""",
            [user_id], many=False
        )
        if not result:
            return None, False
        return result[0]['groupNumber'], result[0].get('isTeamLeader', False)

    def post(self, request):
        try:
            # 1. Input Validation
            attendance_id = request.data.get("attendanceId")
            new_status_input = request.data.get("status")
            overtime_hours_override = request.data.get("overtimeHours")
            tl_reason_text = request.data.get("tlReasonText", "").strip() if request.data.get("tlReasonText") else ""
            tl_voice_files = request.FILES.getlist("tlReasonVoiceNote")

            if attendance_id is None or new_status_input is None:
                return error_response(message="attendanceId and status are required.", status_code=400)

            try:
                new_status_input = int(new_status_input)
            except (ValueError, TypeError):
                return error_response(message="Status must be a valid integer.", status_code=400)

            # Determine caller's role
            caller_role_order, caller_is_tl = self._get_user_role_order(request.user.id)
            is_team_leader = (caller_role_order == GROUP_NUMBER_TEAM_LEADER or caller_is_tl)
            is_supervisor_or_above = (caller_role_order is not None and caller_role_order <= GROUP_NUMBER_SUPERVISOR)

            # 2. Fetch current record state
            record_exists = execute_query(
                """SELECT id, "labourUserId", "checkInTime", "checkOutTime", "assignedShiftAtCheckInId", 
                          "breakInTime", "breakOutTime", "overtimeHours", "emergencyHours", 
                          "overtimeStatus", "attendanceStatus"
                   FROM "attendance" 
                   WHERE "id" = %s AND "isDeleted" = 0""",
                [attendance_id],
                many=False
            )

            if not record_exists:
                return error_response(message="Attendance record not found.", status_code=404)

            record = record_exists[0]

            # Force types
            current_db_status = int(record['overtimeStatus'])
            current_total_ot = float(record['overtimeHours'] or 0.0)
            current_ec_hours = float(record['emergencyHours'] or 0.0)

            # Fetch target employee name for logging
            target_user_id = record['labourUserId']
            user_info = execute_query('SELECT "fullName" FROM "user" WHERE id = %s', [target_user_id], many=False)
            target_employee_name = user_info[0]['fullName'] if user_info else 'N/A'

            # 3. Calculate defaults using helper
            shift_id = record.get('assignedShiftAtCheckInId')
            shift_record = None
            if shift_id:
                shift_result = execute_query('SELECT "startTime", "endTime" FROM shifts WHERE "shiftId" = %s', [shift_id], many=False)
                shift_record = shift_result[0] if shift_result else None

            break_duration = (record['breakOutTime'] - record['breakInTime']) if (record.get('breakInTime') and record.get('breakOutTime')) else timedelta(0)

            calc_details = _calculate_attendance_details(
                checkin_time=record['checkInTime'],
                checkout_time=record.get('checkOutTime'),
                shift_details=shift_record,
                break_duration=break_duration
            )

            # =============================================
            # TEAM LEADER APPROVAL PATH (status → 4 = TL Approved)
            # =============================================
            if is_team_leader and not is_supervisor_or_above:
                # TL can only approve (set to TL_APPROVED) or reject
                if new_status_input == int(STATUS_REJECTED):
                    # TL rejection follows same logic as standard rejection below
                    pass  # Fall through to rejection logic
                elif new_status_input == int(STATUS_APPROVED):
                    # TL sends status=2 (approved) but we route it to 4 (TL Approved)
                    # TL must provide a reason (text or voice)
                    # if not tl_reason_text and not tl_voice_files:
                    #     return error_response(
                    #         message="Team Leader must provide a reason (text or voice note) when approving overtime.",
                    #         status_code=400
                    #     )

                    # Save TL voice files if provided
                    tl_voice_paths = []
                    if tl_voice_files:
                        tl_voice_paths = save_overtime_files(attendance_id, tl_voice_files)

                    # Update overtimeRequests with TL reason
                    ot_update_query = """
                        UPDATE "overtimeRequests"
                        SET "tlReasonText" = %s,
                            "tlReasonVoicePath" = %s,
                            "tlApprovedBy" = %s,
                            "tlApprovedAt" = NOW(),
                            "updatedAt" = NOW()
                        WHERE "attendanceRecordId" = %s AND "isDeleted" = 0
                    """
                    execute_query(ot_update_query, [
                        tl_reason_text or None,
                        json.dumps(tl_voice_paths) if tl_voice_paths else None,
                        request.user.id,
                        attendance_id
                    ], fetch=False)

                    # Set overtimeStatus to TL_APPROVED (4)
                    update_query = """
                        UPDATE "attendance"
                        SET "overtimeStatus" = %s, "overtimeHours" = %s, "updatedAt" = NOW()
                        WHERE "id" = %s
                    """
                    execute_query(update_query, [int(STATUS_TL_APPROVED), overtime_hours_override, attendance_id], fetch=False)

                    log_activity_raw(
                        request=request, category='Overtime', action='TLApproved',
                        performer=request.user, target_employee_name=target_employee_name,
                        details={'attendanceId': attendance_id, 'tlReasonText': tl_reason_text}
                    )

                    return success_response(message="Overtime approved by Team Leader. Awaiting Supervisor approval.")
                else:
                    return error_response(message="Team Leader can only approve or reject overtime.", status_code=400)

            # =============================================
            # REJECTION PATH (any authorized role)
            # =============================================
            if new_status_input == int(STATUS_REJECTED):
                final_status = new_status_input
                final_total_ot = current_total_ot
                final_ec_hours = current_ec_hours
                final_reg_hours = calc_details['regular_hours']
                final_attendance_status = 'Overtime'

                if current_ec_hours > 0:
                    # Emergency rejection: remove EC hours, keep normal OT as approved
                    final_ec_hours = 0.0
                    final_total_ot = max(0.0, current_total_ot - current_ec_hours)
                    final_status = int(STATUS_APPROVED)
                    final_attendance_status = 'Overtime' if final_total_ot > 0 else 'Normal'
                else:
                    # Standard rejection: wipe everything
                    final_total_ot = 0.0
                    final_ec_hours = 0.0
                    final_reg_hours = calc_details.get('shift_duration_hours', calc_details['total_working_hours'])
                    final_attendance_status = 'Normal'

                update_query = """
                    UPDATE "attendance"
                    SET "overtimeStatus" = %s, "overtimeHours" = %s, "emergencyHours" = %s,
                        "attendanceStatus" = %s, "calculatedRegularHours" = %s, "updatedAt" = NOW()
                    WHERE "id" = %s
                """
                execute_query(update_query, [
                    final_status, final_total_ot, final_ec_hours,
                    final_attendance_status, final_reg_hours, attendance_id
                ], fetch=False)

                log_activity_raw(
                    request=request, category='Overtime', action='Rejected',
                    performer=request.user, target_employee_name=target_employee_name,
                    details={'attendanceId': attendance_id, 'newStatus': final_status}
                )

                return success_response(message="Overtime rejected successfully.")

            # =============================================
            # SUPERVISOR / ADMIN FINAL APPROVAL PATH (status → 2 = Approved)
            # =============================================
            if new_status_input == int(STATUS_APPROVED):
                final_total_ot = current_total_ot
                final_reg_hours = calc_details['regular_hours']

                if overtime_hours_override is not None:
                    final_total_ot = float(overtime_hours_override)
                else:
                    final_total_ot = calc_details['overtime_hours']

                final_attendance_status = 'Overtime' if final_total_ot > 0 else 'Normal'

                # Update overtimeRequests with supervisor info
                sup_update_query = """
                    UPDATE "overtimeRequests"
                    SET "supervisorApprovedBy" = %s,
                        "supervisorApprovedAt" = NOW(),
                        "updatedAt" = NOW()
                    WHERE "attendanceRecordId" = %s AND "isDeleted" = 0
                """
                execute_query(sup_update_query, [request.user.id, attendance_id], fetch=False)

                update_query = """
                    UPDATE "attendance"
                    SET "overtimeStatus" = %s, "overtimeHours" = %s, "emergencyHours" = %s,
                        "attendanceStatus" = %s, "calculatedRegularHours" = %s, "updatedAt" = NOW()
                    WHERE "id" = %s
                """
                execute_query(update_query, [
                    int(STATUS_APPROVED), final_total_ot, current_ec_hours,
                    final_attendance_status, final_reg_hours, attendance_id
                ], fetch=False)

                log_activity_raw(
                    request=request, category='Overtime', action='Approved',
                    performer=request.user, target_employee_name=target_employee_name,
                    details={'attendanceId': attendance_id, 'overtimeHours': final_total_ot}
                )

                return success_response(message="Overtime final approved successfully.")

            return error_response(message="Invalid status value.", status_code=400)

        except Exception as e:
            return error_response(message=str(e), status_code=500)

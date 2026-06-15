from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, save_leave_certificate, log_activity_raw, _send_notification, get_leave_type_name
from api.messages import *
from api.constants import *
from django.conf import settings
from datetime import datetime, timedelta
import traceback
import json

# Import Celery task for certificate deadline checking
try:
    from api.tasks import check_leave_certificate_deadline
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    print("WARNING: Celery tasks not available. Certificate deadline checks will not be scheduled.")


class LeaveApplicationView(APIView):
    """
    Handles CRUD operations for Leave Applications.
    - Employees can apply for leave and view their applications
    - Supervisors and Team Leaders can approve/reject leave requests
    """
    permission_classes = [IsAuthenticated]
    allowed_sort_fields = [
        "id", "leaveId", "employeeName", "leaveType", "startDate", 
        "endDate", "totalDays", "leaveStatus", "createdAt"
    ]

    def _parse_leave_date(self, value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
            return value
        return None

    def _leave_type_to_attendance_status(self, leave_type):
        leave_type_map = {
            LEAVE_TYPE_EMERGENCY: 'Emergency Leave',
            LEAVE_TYPE_ANNUAL: 'Annual Leave',
            LEAVE_TYPE_SICK: 'Sick Leave',
        }
        return leave_type_map.get(leave_type)

    def _upsert_attendance_status(self, labour_user_id, attendance_date, attendance_status, shift_id=None, insert_if_missing=True):
        attendance_query = '''
            SELECT id, "attendanceStatus"
            FROM attendance
            WHERE "labourUserId" = %s
              AND "date" = %s
              AND "isDeleted" = 0
            LIMIT 1
        '''
        attendance_result = execute_query(attendance_query, [labour_user_id, attendance_date], fetch='one')
        if isinstance(attendance_result, list) and attendance_result:
            attendance_result = attendance_result[0]

        if attendance_result:
            current_status = attendance_result.get('attendanceStatus')
            if current_status in {'Normal', 'Halfday', 'Overtime'}:
                return

            update_query = '''
                UPDATE attendance
                SET "attendanceStatus" = %s,
                    "calculatedRegularHours" = 0,
                    "assignedShiftAtCheckInId" = COALESCE(%s, "assignedShiftAtCheckInId"),
                    "updatedAt" = NOW()
                WHERE id = %s AND "isDeleted" = 0
            '''
            execute_query(update_query, [attendance_status, shift_id, attendance_result.get('id')])
            return

        if not insert_if_missing:
            return

        insert_query = '''
            INSERT INTO attendance
            ("labourUserId", "date", "attendanceStatus", "calculatedRegularHours", "assignedShiftAtCheckInId", "isDeleted", "createdAt", "updatedAt")
            VALUES (%s, %s, %s, %s, %s, 0, NOW(), NOW())
        '''
        execute_query(insert_query, [labour_user_id, attendance_date, attendance_status, 0, shift_id])

    def _reconcile_moved_approved_leave(self, employee_id, leave_type, old_start_value, old_end_value, new_start_value, new_end_value):
        old_start_date = self._parse_leave_date(old_start_value)
        old_end_date = self._parse_leave_date(old_end_value)
        new_start_date = self._parse_leave_date(new_start_value)
        new_end_date = self._parse_leave_date(new_end_value)

        if not employee_id or not old_start_date or not old_end_date or not new_start_date or not new_end_date:
            return

        if old_end_date < old_start_date or new_end_date < new_start_date:
            return

        approved_status = self._leave_type_to_attendance_status(leave_type)
        if not approved_status:
            return

        employee_lookup_query = '''
            SELECT id, "shiftId"
            FROM "user"
            WHERE "employeeId" = %s AND COALESCE("isDeleted", 0) = 0
            LIMIT 1
        '''
        employee_result = execute_query(employee_lookup_query, [employee_id], fetch='one')
        if isinstance(employee_result, list) and employee_result:
            employee_result = employee_result[0]

        if not employee_result or not employee_result.get('id'):
            return

        labour_user_id = employee_result.get('id')
        shift_id = employee_result.get('shiftId')
        today = datetime.now().date()

        old_dates = set()
        current_date = old_start_date
        while current_date <= old_end_date:
            old_dates.add(current_date)
            current_date += timedelta(days=1)

        new_dates = set()
        current_date = new_start_date
        while current_date <= new_end_date:
            new_dates.add(current_date)
            current_date += timedelta(days=1)

        dates_to_restore = sorted(old_dates - new_dates)
        dates_to_apply = sorted(new_dates)

        for attendance_date in dates_to_restore:
            self._upsert_attendance_status(
                labour_user_id,
                attendance_date,
                'Absent',
                shift_id=shift_id,
                insert_if_missing=attendance_date <= today
            )

        for attendance_date in dates_to_apply:
            self._upsert_attendance_status(
                labour_user_id,
                attendance_date,
                approved_status,
                shift_id=shift_id,
                insert_if_missing=True
            )

    def get(self, request, leave_id=None):
        """
        Fetch leave applications with filtering, sorting, and pagination.
        Employees see only their leaves, Supervisors/Team Leaders see their team's leaves.
        """
        try:
            user = request.user
            
            # Get user's groupNumber, roleOrderId, and isTeamLeader from their role
            user_group_query = """
                SELECT ur."groupNumber", ur."roleOrderId", ur."isTeamLeader"
                FROM "user" u
                JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE u.id = %s
            """
            user_group_result = execute_query(user_group_query, [user.id], many=False)
            
            user_group_number = None
            logged_in_user_role_order_id = None
            is_logged_in_user_team_leader = False
            if user_group_result:
                user_group_number = user_group_result[0].get('groupNumber')
                logged_in_user_role_order_id = user_group_result[0].get('roleOrderId')
                is_logged_in_user_team_leader = user_group_result[0].get('isTeamLeader', False)
            
            # print(f"DEBUG: User {user.id} has groupNumber={user_group_number}, isTeamLeader={is_logged_in_user_team_leader}")

            if leave_id:
                # Fetch single leave application
                query = """
                    SELECT 
                        la.id, la."leaveId", la."employeeId", la."leaveType", 
                        la."emergencyReason", la."startDate", la."endDate", la."reportingDate",
                        la."contactAddress", la."contactNumber", la."leaveCertificate",
                        la."leaveStatus", la."certificateUploadDeadline", 
                        la."approvedBy", la."approvedAt", la."rejectionReason", la."totalDays",
                        la."respondedByTeamLeader", la."respondedByTeamLeaderAt",
                        la."respondedBySupervisor", la."respondedBySupervisorAt",
                        la."respondedByHR", la."respondedByHRAt",
                        la."rejectedBy", la."rejectedAt",
                        la."createdAt", la."updatedAt",
                        u."fullName" AS "employeeName", u."employeeId",
                        u."phoneNumber", u.department,
                        approver."fullName" AS "approverName",
                        tl_responder."fullName" AS "teamLeaderResponderName",
                        sup_responder."fullName" AS "supervisorResponderName",
                        hr_responder."fullName" AS "hrResponderName",
                        CASE 
                            WHEN la."rejectedBy" = 'SYSTEM' THEN 'System'
                            ELSE rejector."fullName"
                        END AS "rejectedByName"
                    FROM "leaveApplication" la
                    LEFT JOIN "user" u ON la."employeeId" = u."employeeId" AND COALESCE(u."isDeleted", '0') = '0'
                    LEFT JOIN "user" approver ON la."approvedBy" = approver."employeeId" AND COALESCE(approver."isDeleted", '0') = '0'
                    LEFT JOIN "user" tl_responder ON la."respondedByTeamLeader" = tl_responder."employeeId" AND COALESCE(tl_responder."isDeleted", '0') = '0'
                    LEFT JOIN "user" sup_responder ON la."respondedBySupervisor" = sup_responder."employeeId" AND COALESCE(sup_responder."isDeleted", '0') = '0'
                    LEFT JOIN "user" hr_responder ON la."respondedByHR" = hr_responder."employeeId" AND COALESCE(hr_responder."isDeleted", '0') = '0'
                    LEFT JOIN "user" rejector ON la."rejectedBy" = rejector."employeeId" AND COALESCE(rejector."isDeleted", '0') = '0'
                    WHERE la.id = %s AND COALESCE(la."isDeleted", 0) = 0
                """
                result = execute_query(query, [leave_id], fetch='one')
                
                if isinstance(result, list) and result:
                    record = result[0]
                else:
                    record = result
                
                if not record:
                    return error_response(message=LEAVE_APPLICATION_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)
                
                # Build approval chain history
                # Skip auto-approved stages (when applicant approved their own stage)
                applicant_employee_id = record.get('employeeId')
                approval_chain = []
                
                # Add Team Leader approval only if NOT auto-approved by applicant
                if record.get('respondedByTeamLeader') and record.get('respondedByTeamLeader') != applicant_employee_id:
                    approval_chain.append({
                        'stage': 1,
                        'role': 'Team Leader',
                        'respondedBy': record.get('respondedByTeamLeader'),
                        'responderName': record.get('teamLeaderResponderName'),
                        'respondedAt': record.get('respondedByTeamLeaderAt')
                    })
                
                # Add Supervisor approval only if NOT auto-approved by applicant
                if record.get('respondedBySupervisor') and record.get('respondedBySupervisor') != applicant_employee_id:
                    approval_chain.append({
                        'stage': 2,
                        'role': 'Supervisor',
                        'respondedBy': record.get('respondedBySupervisor'),
                        'responderName': record.get('supervisorResponderName'),
                        'respondedAt': record.get('respondedBySupervisorAt')
                    })
                
                # Always add HR approval (HR never auto-approves their own leave in this system)
                if record.get('respondedByHR'):
                    approval_chain.append({
                        'stage': 3,
                        'role': 'HR',
                        'respondedBy': record.get('respondedByHR'),
                        'responderName': record.get('hrResponderName'),
                        'respondedAt': record.get('respondedByHRAt')
                    })
                
                record['approvalChain'] = approval_chain
                
                # Add rejection info if rejected
                if record.get('leaveStatus') == LEAVE_STATUS_REJECTED and record.get('rejectedBy'):
                    record['rejectionInfo'] = {
                        'rejectedBy': record.get('rejectedBy'),
                        'rejectedByName': record.get('rejectedByName'),
                        'rejectedAt': record.get('rejectedAt'),
                        'rejectionReason': record.get('rejectionReason'),
                        'rejectedAtStage': len(approval_chain)  # Shows at which stage it was rejected
                    }
                
                # Build absolute URL for certificate if exists
                if record.get('leaveCertificate'):
                    record['leaveCertificate'] = request.build_absolute_uri(
                        settings.MEDIA_URL + record['leaveCertificate']
                    )
                
                # Calculate time remaining for certificate upload
                if record.get('certificateUploadDeadline'):
                    deadline = record['certificateUploadDeadline']
                    if isinstance(deadline, str):
                        deadline = datetime.fromisoformat(deadline)
                    time_remaining = (deadline - datetime.now()).total_seconds() / 3600  # hours
                    record['timeRemainingForCertificate'] = round(max(0, time_remaining), 2)
                else:
                    record['timeRemainingForCertificate'] = None
                
                return success_response(data=record, message=LEAVE_FETCHED_SUCCESSFULLY)

            # Fetch list of leave applications
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            search = request.query_params.get("search", "").strip()
            sort_param = request.query_params.get("sort", "").strip()
            
            # Filters
            leave_type = request.query_params.get("leaveType", "").strip()
            leave_status = request.query_params.get("leaveStatus", "").strip()
            employee_id = request.query_params.get("employeeId", "").strip()
            start_date = request.query_params.get("startDate", "").strip()
            end_date = request.query_params.get("endDate", "").strip()
            filter_by_reporting_to_id = request.query_params.get("filterByReportingToId", "").strip()

            params = []
            where_conditions = ['COALESCE(la."isDeleted", 0) = 0']

            # ===== HIERARCHICAL FILTERING BASED ON ROLE ORDER ID =====
            # This handles multi-level reporting: Supervisor → Team Leader → Workers
            # Similar logic to attendance list view
            # NOTE: Skip hierarchical filtering if employeeId parameter is explicitly provided
            
            allowed_employee_ids = []
            logged_in_user_id = user.id
            
            # Determine if we're filtering by a specific supervisor or using logged-in user's hierarchy
            # Convert to int since filterByReportingToId now expects user.id (integer) not employeeId (string)
            filter_by_reporting_to_id_param = int(filter_by_reporting_to_id) if filter_by_reporting_to_id else 0

            team_available = """
                SELECT * FROM "teamManagement" Where "teamLeaderId" = %s AND "isDeleted" = 0"""
            
            team_available_result = execute_query(team_available, [logged_in_user_id], many=False)


            # If employeeId parameter is provided (now expects user.id as integer), convert to string employeeId
            employee_id_string = None
            if employee_id:
                try:
                    employee_id_int = int(employee_id)
                    # Look up the string employeeId from user.id
                    employee_lookup_query = """
                        SELECT "employeeId" FROM "user" WHERE id = %s AND COALESCE("isDeleted", 0) = 0
                    """
                    employee_lookup_result = execute_query(employee_lookup_query, [employee_id_int], many=False)
                    if employee_lookup_result:
                        employee_id_string = employee_lookup_result[0].get('employeeId')
                except (ValueError, TypeError):
                    # If not a valid integer, treat as string employeeId (backward compatibility)
                    employee_id_string = employee_id

            # --- START HIERARCHICAL REPORTING LOGIC ---
            # Skip hierarchical filtering if employeeId is explicitly provided
            if not employee_id_string:
                # If the logged-in user is a Supervisor (roleOrderId = 3) and NOT a Team Leader
                if logged_in_user_role_order_id == 3 and not is_logged_in_user_team_leader and not filter_by_reporting_to_id_param:
                    # Supervisors: See all subordinates recursively (Team Leaders + Workers under them)
                    # But EXCLUDE their own leave applications
                    print(f">>> Supervisor: Showing all team members (excluding self)")
                    recursive_subordinate_query = """
                        WITH RECURSIVE subordinates AS (
                            -- Start with users directly reporting to the supervisor
                            SELECT id, "employeeId", "reportingToId", "roleId"
                            FROM "user"
                            WHERE "reportingToId" = %s 
                              AND "isDeleted" = 0
                            
                            UNION ALL
                            
                            -- Recursively find users reporting to subordinates (e.g., Workers under Team Leaders)
                            SELECT u.id, u."employeeId", u."reportingToId", u."roleId"
                            FROM "user" u
                            INNER JOIN subordinates s ON u."reportingToId" = s.id
                            WHERE u."isDeleted" = 0
                        )
                        SELECT DISTINCT "employeeId" FROM subordinates;
                    """
                    subordinate_results = execute_query(recursive_subordinate_query, [logged_in_user_id], many=True)
                    allowed_employee_ids = [res['employeeId'] for res in subordinate_results]

                    if allowed_employee_ids:
                        placeholders = ','.join(['%s'] * len(allowed_employee_ids))
                        where_conditions.append(f'la."employeeId" IN ({placeholders})')
                        params.extend(allowed_employee_ids)
                    else:
                        # No subordinates, show no records
                        where_conditions.append('1 = 0')

                elif is_logged_in_user_team_leader and not filter_by_reporting_to_id_param:
                    # Team Leader logic with two modes:
                    # A) If team exists in teamManagement: Show ONLY their own direct team members
                    # B) Otherwise: Show all users under same supervisor (fallback)

                    if team_available_result:
                        # isTeamLeader=true with team: Only show users in this team leader's team
                        print(f">>> Team Leader: Showing only direct team members")
                        recursive_subordinate_query = """
                            WITH RECURSIVE subordinates AS (
                                SELECT id, "employeeId", "reportingToId", "roleId"
                                FROM "user"
                                WHERE "teamLeaderId" = %s
                                  AND "isDeleted" = 0
                                
                                UNION ALL
                                
                                SELECT u.id, u."employeeId", u."reportingToId", u."roleId"
                                FROM "user" u
                                INNER JOIN subordinates s ON u."teamLeaderId" = s.id
                                WHERE u."isDeleted" = 0
                            )
                            SELECT DISTINCT "employeeId" FROM subordinates;
                        """
                        subordinate_results = execute_query(recursive_subordinate_query, [logged_in_user_id], many=True)
                        allowed_employee_ids = [res['employeeId'] for res in subordinate_results]

                        if allowed_employee_ids:
                            placeholders = ','.join(['%s'] * len(allowed_employee_ids))
                            where_conditions.append(f'la."employeeId" IN ({placeholders})')
                            params.extend(allowed_employee_ids)
                        else:
                            where_conditions.append('1 = 0')
                    else:
                        # Fallback: Show all users under the SAME SUPERVISOR
                        print(f">>> Team Leader (no team): Showing all team members under same supervisor (excluding self)")
                        
                        team_leader_supervisor_query = """
                            SELECT "reportingToId" FROM "user" WHERE id = %s
                        """
                        supervisor_result = execute_query(team_leader_supervisor_query, [logged_in_user_id], many=False)
                        
                        if supervisor_result and supervisor_result[0].get('reportingToId'):
                            supervisor_id = supervisor_result[0]['reportingToId']
                            
                            recursive_subordinate_query = """
                                WITH RECURSIVE subordinates AS (
                                    SELECT id, "employeeId", "reportingToId", "roleId"
                                    FROM "user"
                                    WHERE "reportingToId" = %s
                                      AND "isDeleted" = 0
                                    
                                    UNION ALL
                                    
                                    SELECT u.id, u."employeeId", u."reportingToId", u."roleId"
                                    FROM "user" u
                                    INNER JOIN subordinates s ON u."reportingToId" = s.id
                                    WHERE u."isDeleted" = 0
                                )
                                SELECT DISTINCT "employeeId" FROM subordinates;
                            """
                            subordinate_results = execute_query(recursive_subordinate_query, [supervisor_id], many=True)
                            allowed_employee_ids = [res['employeeId'] for res in subordinate_results]
                            
                            logged_in_employee_id = getattr(user, 'employeeId', None)
                            if logged_in_employee_id and logged_in_employee_id in allowed_employee_ids:
                                allowed_employee_ids.remove(logged_in_employee_id)
                            
                            if allowed_employee_ids:
                                placeholders = ','.join(['%s'] * len(allowed_employee_ids))
                                where_conditions.append(f'la."employeeId" IN ({placeholders})')
                                params.extend(allowed_employee_ids)
                            else:
                                where_conditions.append('1 = 0')
                        else:
                            where_conditions.append('1 = 0')

                elif filter_by_reporting_to_id_param and filter_by_reporting_to_id_param > 0:
                    # This block is for when filterByReportingToId is explicitly provided
                    # Need to check if the passed ID is a Supervisor or Team Leader
                    print(f">>> FilterByReportingToId: Checking role of user ID {filter_by_reporting_to_id_param}")
                    
                    # Get the target user's details from their id (integer) including isTeamLeader flag
                    target_user_query = """
                        SELECT u.id, u."employeeId", u."reportingToId", ur."roleOrderId", ur."isTeamLeader"
                        FROM "user" u
                        JOIN "userrole" ur ON u."roleId" = ur."roleId"
                        WHERE u.id = %s AND COALESCE(u."isDeleted", 0) = 0
                    """
                    target_user_result = execute_query(target_user_query, [filter_by_reporting_to_id_param], many=False)
                    
                    if target_user_result:
                        target_user_id = target_user_result[0].get('id')
                        target_is_team_leader = target_user_result[0].get('isTeamLeader', False)
                        target_reporting_to_id = target_user_result[0].get('reportingToId')
                        
                        # If the passed ID is a Team Leader (check isTeamLeader flag)
                        # Check if the TARGET user has a team (not the logged-in user)
                        target_team_available = """
                            SELECT * FROM "teamManagement" WHERE "teamLeaderId" = %s AND "isDeleted" = 0
                        """
                        target_team_result = execute_query(target_team_available, [filter_by_reporting_to_id_param], many=False)

                        if target_is_team_leader and target_team_result:
                            # Show only the team leader's direct team members
                            print(f">>> FilterByReportingToId (Team Leader): Showing only direct team members")
                            
                            recursive_subordinate_query = """
                                WITH RECURSIVE subordinates AS (
                                    SELECT id, "employeeId", "reportingToId", "roleId"
                                    FROM "user"
                                    WHERE "teamLeaderId" = %s
                                      AND "isDeleted" = 0
                                    
                                    UNION ALL
                                    
                                    SELECT u.id, u."employeeId", u."reportingToId", u."roleId"
                                    FROM "user" u
                                    INNER JOIN subordinates s ON u."teamLeaderId" = s.id
                                    WHERE u."isDeleted" = 0
                                )
                                SELECT DISTINCT "employeeId" FROM subordinates;
                            """
                            subordinate_results = execute_query(recursive_subordinate_query, [filter_by_reporting_to_id_param], many=True)
                            allowed_employee_ids = [res['employeeId'] for res in subordinate_results]
                                
                            if allowed_employee_ids:
                                placeholders = ','.join(['%s'] * len(allowed_employee_ids))
                                where_conditions.append(f'la."employeeId" IN ({placeholders})')
                                params.extend(allowed_employee_ids)
                            else:
                                where_conditions.append('1 = 0')
                        
                        # If the passed ID is a Supervisor (roleOrderId = 3) or Admin
                        else:
                            # Show all subordinates recursively (normal behavior)
                            print(f">>> FilterByReportingToId (Supervisor/Admin): Showing all subordinates")
                            
                            recursive_subordinate_query = """
                                WITH RECURSIVE subordinates AS (
                                    -- Start with users directly reporting to the specified user
                                    SELECT id, "employeeId", "reportingToId", "roleId"
                                    FROM "user"
                                    WHERE "reportingToId" = %s
                                      AND "isDeleted" = 0
                                    
                                    UNION ALL
                                    
                                    -- Recursively find users reporting to subordinates
                                    SELECT u.id, u."employeeId", u."reportingToId", u."roleId"
                                    FROM "user" u
                                    INNER JOIN subordinates s ON u."reportingToId" = s.id
                                    WHERE u."isDeleted" = 0
                                )
                                SELECT DISTINCT "employeeId" FROM subordinates;
                            """
                            subordinate_results = execute_query(recursive_subordinate_query, [target_user_id], many=True)
                            allowed_employee_ids = [res['employeeId'] for res in subordinate_results]

                            if allowed_employee_ids:
                                placeholders = ','.join(['%s'] * len(allowed_employee_ids))
                                where_conditions.append(f'la."employeeId" IN ({placeholders})')
                                params.extend(allowed_employee_ids)
                            else:
                                # No subordinates found, show no records
                                where_conditions.append('1 = 0')
                    else:
                        # Invalid employeeId provided, show no records
                        where_conditions.append('1 = 0')
            
            # else: Admin level (roleOrderId 1-2) or Workers - no additional hierarchy filter
            # Admins see all, Workers see only their own (handled by other filters)

            # Apply additional filters
            if search:
                where_conditions.append("""
                    (
                        la."leaveId" ILIKE %s OR
                        u."fullName" ILIKE %s OR
                        u."employeeId" ILIKE %s OR
                        la."emergencyReason" ILIKE %s
                    )
                """)
                search_param = f"%{search}%"
                params.extend([search_param] * 4)

            if employee_id_string:
                where_conditions.append('la."employeeId" ILIKE %s')
                params.append(f"%{employee_id_string}%")

            if leave_type:
                where_conditions.append('la."leaveType" = %s')
                params.append(int(leave_type))

            if leave_status:
                where_conditions.append('la."leaveStatus" = %s')
                params.append(int(leave_status))

            if start_date:
                where_conditions.append('la."startDate" >= %s')
                params.append(start_date)

            if end_date:
                where_conditions.append('la."endDate" <= %s')
                params.append(end_date)

            where_clause = "WHERE " + " AND ".join(where_conditions)

            # Sorting
            order_by = 'ORDER BY la.id DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    sort_column_mapping = {
                        "id": 'la.id',
                        "leaveId": 'la."leaveId"',
                        "employeeName": 'u."fullName"',
                        "leaveType": 'la."leaveType"',
                        "startDate": 'la."startDate"',
                        "endDate": 'la."endDate"',
                        "totalDays": 'la."totalDays"',
                        "leaveStatus": 'la."leaveStatus"',
                        "createdAt": 'la."createdAt"'
                    }
                    if sort_field == "employeeName":
                        order_by = f'ORDER BY LOWER(TRIM({sort_column_mapping[sort_field]})) {sort_direction}'
                    else:
                        order_by = f'ORDER BY {sort_column_mapping[sort_field]} {sort_direction}'

            # Count query
            count_query = f"""
                SELECT COUNT(la.id) AS total 
                FROM "leaveApplication" la
                LEFT JOIN "user" u ON la."employeeId" = u."employeeId" AND COALESCE(u."isDeleted", '0') = '0'
                {where_clause}
            """
            total_result = execute_query(count_query, list(params), fetch='one')
            
            total_count = 0
            if isinstance(total_result, list) and total_result:
                total_count = total_result[0].get("total", 0)
            elif isinstance(total_result, dict):
                total_count = total_result.get("total", 0)
            
            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 and not is_export else 1

            # Main query
            query = f"""
                SELECT 
                    la.id, la."leaveId", la."employeeId", la."leaveType", 
                    la."startDate", la."endDate", la."totalDays", la."leaveStatus",
                    la."createdAt", la."leaveCertificate", la."emergencyReason",
                    la."reportingDate", la."contactAddress", la."contactNumber",
                    la."respondedByTeamLeader", la."respondedByTeamLeaderAt",
                    la."respondedBySupervisor", la."respondedBySupervisorAt",
                    la."respondedByHR", la."respondedByHRAt",
                    la."rejectedBy", la."rejectedAt", la."rejectionReason",
                    u."fullName" AS "employeeName", u."phoneNumber", u.department, u."nationality", u."dateOfJoining", u."employmentStatus",
                    approver."fullName" AS "approverName",
                    tl_responder."fullName" AS "teamLeaderResponderName",
                    sup_responder."fullName" AS "supervisorResponderName",
                    hr_responder."fullName" AS "hrResponderName",
                    CASE 
                        WHEN la."rejectedBy" = 'SYSTEM' THEN 'System'
                        ELSE rejector."fullName"
                    END AS "rejectedByName"
                FROM "leaveApplication" la
                LEFT JOIN "user" u ON la."employeeId" = u."employeeId" AND COALESCE(u."isDeleted", '0') = '0'
                LEFT JOIN "user" approver ON la."approvedBy" = approver."employeeId" AND COALESCE(approver."isDeleted", '0') = '0'
                LEFT JOIN "user" tl_responder ON la."respondedByTeamLeader" = tl_responder."employeeId" AND COALESCE(tl_responder."isDeleted", '0') = '0'
                LEFT JOIN "user" sup_responder ON la."respondedBySupervisor" = sup_responder."employeeId" AND COALESCE(sup_responder."isDeleted", '0') = '0'
                LEFT JOIN "user" hr_responder ON la."respondedByHR" = hr_responder."employeeId" AND COALESCE(hr_responder."isDeleted", '0') = '0'
                LEFT JOIN "user" rejector ON la."rejectedBy" = rejector."employeeId" AND COALESCE(rejector."isDeleted", '0') = '0'
                {where_clause}
                {order_by}
            """

            if not is_export:
                query += ' LIMIT %s OFFSET %s'
                params.extend([page_size, (page - 1) * page_size])

            records = execute_query(query, params, many=True)
            if not isinstance(records, list):
                records = []

            for record in records:
                # Build approval chain for each record
                # Skip auto-approved stages (when applicant approved their own stage)
                applicant_employee_id = record.get('employeeId')
                approval_chain = []
                
                # Add Team Leader approval only if NOT auto-approved by applicant
                if record.get('respondedByTeamLeader') and record.get('respondedByTeamLeader') != applicant_employee_id:
                    approval_chain.append({
                        'stage': 1,
                        'role': 'Team Leader',
                        'respondedBy': record.get('respondedByTeamLeader'),
                        'responderName': record.get('teamLeaderResponderName'),
                        'respondedAt': record.get('respondedByTeamLeaderAt')
                    })
                
                # Add Supervisor approval only if NOT auto-approved by applicant
                if record.get('respondedBySupervisor') and record.get('respondedBySupervisor') != applicant_employee_id:
                    approval_chain.append({
                        'stage': 2,
                        'role': 'Supervisor',
                        'respondedBy': record.get('respondedBySupervisor'),
                        'responderName': record.get('supervisorResponderName'),
                        'respondedAt': record.get('respondedBySupervisorAt')
                    })
                
                # Always add HR approval (HR never auto-approves their own leave in this system)
                if record.get('respondedByHR'):
                    approval_chain.append({
                        'stage': 3,
                        'role': 'HR',
                        'respondedBy': record.get('respondedByHR'),
                        'responderName': record.get('hrResponderName'),
                        'respondedAt': record.get('respondedByHRAt')
                    })
                
                record['approvalChain'] = approval_chain
                
                # Add rejection info if rejected
                if record.get('leaveStatus') == LEAVE_STATUS_REJECTED and record.get('rejectedBy'):
                    record['rejectionInfo'] = {
                        'rejectedBy': record.get('rejectedBy'),
                        'rejectedByName': record.get('rejectedByName', 'SYSTEM'),
                        'rejectedAt': record.get('rejectedAt'),
                        'rejectionReason': record.get('rejectionReason'),
                        'rejectedAtStage': len(approval_chain)
                    }
                
                if record.get('leaveCertificate'):
                    record['leaveCertificate'] = request.build_absolute_uri(
                        settings.MEDIA_URL + record['leaveCertificate']
                    )

            response_data = {
                "results": records,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page if not is_export else 1,
                    "pageSize": page_size if not is_export else total_count,
                }
            }
            
            return success_response(data=response_data, message=LEAVES_FETCHED_SUCCESSFULLY)

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"{ERROR_FETCHING_LEAVES}: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):
        """
        Create a new leave application.
        Automatically calculates total days and sets certificate deadline for sick leaves.
        """
        try:
            user = request.user
            data = request.POST
            certificate_file = request.FILES.get('leaveCertificate')

            # Validate required fields
            required_fields = ["leaveType", "startDate", "endDate"]
            if not all(field in data for field in required_fields):
                return error_response(
                    message=LEAVE_DATES_REQUIRED,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            leave_type = int(data.get('leaveType'))
            start_date = data.get('startDate')
            end_date = data.get('endDate')

            # Validate dates
            try:
                e = datetime.strptime(start_date, "%Y-%m-%d")
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                
                if end_dt < e:
                    return error_response(
                        message=END_DATE_BEFORE_START_DATE,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                
                # Calculate total days
                total_days = (end_dt - e).days + 1
                
            except ValueError:
                return error_response(
                    message=INVALID_DATE_FORMAT_YYYYMMDD,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Leave application requires team assignment for regular employees.
            # Exception: Team Leaders and Supervisors (roleOrderId = 3) can apply even without team membership.
            team_and_role_query = """
                SELECT u."teamLeaderId", ur."isTeamLeader", ur."roleOrderId"
                FROM "user" u
                JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE u.id = %s AND COALESCE(u."isDeleted", 0) = 0
            """
            team_and_role_result = execute_query(team_and_role_query, [user.id], many=False)

            if team_and_role_result:
                user_team_leader_id = team_and_role_result[0].get('teamLeaderId')
                user_is_team_leader = team_and_role_result[0].get('isTeamLeader', False)
                user_role_order_id = team_and_role_result[0].get('roleOrderId')

                is_supervisor = user_role_order_id == 3
                if user_team_leader_id is None and not user_is_team_leader and not is_supervisor:
                    return error_response(
                        message=LEAVE_APPLICATION_NOT_ALLOWED,
                        status_code=status.HTTP_403_FORBIDDEN
                    )


            # Validate emergency reason for emergency leaves
            if leave_type == LEAVE_TYPE_EMERGENCY and not data.get('emergencyReason'):
                return error_response(
                    message=EMERGENCY_REASON_REQUIRED,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Check for overlapping leave dates (ANY leave type)
            # User cannot apply for leaves with overlapping dates regardless of type
            overlap_check_query = """
                SELECT "leaveId", "leaveType", "startDate", "endDate"
                FROM "leaveApplication"
                WHERE "employeeId" = %s
                AND COALESCE("isDeleted", 0) = 0
                AND "leaveStatus" != %s
                AND (
                    ("startDate" <= %s AND "endDate" >= %s)
                    OR ("startDate" <= %s AND "endDate" >= %s)
                    OR ("startDate" >= %s AND "endDate" <= %s)
                )
                LIMIT 1
            """
            overlap_result = execute_query(
                overlap_check_query,
                [
                    getattr(user, 'employeeId', None),
                    LEAVE_STATUS_REJECTED,   # Exclude rejected leaves
                    end_date, start_date,    # Existing leave ends after new starts
                    end_date, end_date,      # Existing leave starts before new ends
                    start_date, end_date     # Existing leave is within new leave dates
                ],
                fetch='one'
            )
            
            if overlap_result:
                existing_leave = overlap_result[0] if isinstance(overlap_result, list) else overlap_result
                leave_type_names = {
                    LEAVE_TYPE_EMERGENCY: "Emergency",
                    LEAVE_TYPE_ANNUAL: "Annual",
                    LEAVE_TYPE_SICK: "Sick"
                }
                existing_type = leave_type_names.get(existing_leave.get('leaveType'), "Leave")
                return error_response(
                    message=f"Cannot apply for leave. You already have a {existing_type} leave for this dates: {existing_leave.get('startDate')} to {existing_leave.get('endDate')}",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Generate unique leave ID
            now = datetime.now()
            year_short = now.strftime('%y')
            month = now.strftime('%m')
            prefix = f"LV-{year_short}-{month}-"

            last_id_query = """
                SELECT "leaveId" FROM "leaveApplication"
                WHERE "leaveId" LIKE %s
                ORDER BY "leaveId" DESC
                LIMIT 1
            """
            last_id_result = execute_query(last_id_query, [f"{prefix}%"], fetch='one')

            if last_id_result:
                last_record = last_id_result[0] if isinstance(last_id_result, list) else last_id_result
                if last_record.get('leaveId'):
                    last_seq_num = int(last_record['leaveId'].split('-')[-1])
                    new_seq_num = last_seq_num + 1
                else:
                    new_seq_num = 1
            else:
                new_seq_num = 1

            new_leave_id = f"{prefix}{new_seq_num:06d}"

            # Calculate certificate upload deadline for sick leaves
            certificate_deadline = None
            if leave_type == LEAVE_TYPE_SICK:
                certificate_deadline = datetime.now() + timedelta(hours=LEAVE_CERTIFICATE_DEADLINE_HOURS)

            # Get reporting date (default to day after end date)
            reporting_date = data.get('reportingDate')
            if not reporting_date:
                reporting_date = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")

            # Insert leave application
            insert_query = """
                INSERT INTO "leaveApplication" (
                    "leaveId", "employeeId", "employeeName", "leaveType", "emergencyReason",
                    "startDate", "endDate", "reportingDate", "contactAddress",
                    "contactNumber", "leaveCertificate", "leaveStatus",
                    "certificateUploadDeadline", "totalDays", "isDeleted",
                    "createdAt", "updatedAt"
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            """
            
            params = [
                new_leave_id,
                getattr(user, 'employeeId', None),
                getattr(user, 'fullName', getattr(user, 'userName', 'Unknown')),
                leave_type,
                data.get('emergencyReason'),
                start_date,
                end_date,
                reporting_date,
                data.get('contactAddress'),
                data.get('contactNumber'),
                None,  # Certificate path (will update if file provided)
                LEAVE_STATUS_PENDING,
                certificate_deadline,
                total_days,
                0,
                datetime.now(),
                datetime.now()
            ]

            result = execute_query(insert_query, params, fetch=True)
            new_id = result[0].get("id") if result else None

            if not new_id:
                return error_response(
                    message=FAILED_TO_CREATE_LEAVE,
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Auto-approve logic: If Team Leader or Supervisor applies, auto-approve their own stage
            auto_approved_status = LEAVE_STATUS_PENDING
            next_approver_role = "Team Leader"  # Default: needs TL approval
            
            # Initialize variables for later use
            applicant_reporting_to_id = None
            applicant_employee_id = getattr(user, 'employeeId', None)
            current_time = datetime.now()
            
            # Get applicant's role information
            applicant_role_query = """
                SELECT ur."roleOrderId", ur."isTeamLeader", u."reportingToId"
                FROM "user" u
                JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE u.id = %s
            """
            applicant_role_result = execute_query(applicant_role_query, [user.id], fetch='one')
            
            if applicant_role_result:
                if isinstance(applicant_role_result, list):
                    applicant_role_data = applicant_role_result[0]
                else:
                    applicant_role_data = applicant_role_result
                
                is_applicant_team_leader = applicant_role_data.get('isTeamLeader', False)
                applicant_role_order_id = applicant_role_data.get('roleOrderId')
                applicant_reporting_to_id = applicant_role_data.get('reportingToId')
                
                # If applicant is a Supervisor (roleOrderId = 3, not team leader)
                if applicant_role_order_id == 3 and not is_applicant_team_leader:
                    # Auto-approve both Team Leader and Supervisor stages
                    auto_approved_status = LEAVE_STATUS_SUPERVISOR_APPROVED
                    next_approver_role = "HR"
                    
                    # Update leave with auto-approvals
                    auto_approve_query = """
                        UPDATE "leaveApplication"
                        SET "leaveStatus" = %s,
                            "respondedByTeamLeader" = %s,
                            "respondedByTeamLeaderAt" = %s,
                            "respondedBySupervisor" = %s,
                            "respondedBySupervisorAt" = %s,
                            "updatedAt" = %s
                        WHERE id = %s
                    """
                    execute_query(auto_approve_query, [
                        auto_approved_status,
                        applicant_employee_id,  # Self-approved as TL
                        current_time,
                        applicant_employee_id,  # Self-approved as Supervisor
                        current_time,
                        current_time,
                        new_id
                    ])
                    print(f"[AUTO-APPROVE] Supervisor {applicant_employee_id} leave auto-approved through TL & Supervisor stages")
                
                # If applicant is a Team Leader
                elif is_applicant_team_leader:
                    # Auto-approve Team Leader stage only
                    auto_approved_status = LEAVE_STATUS_TL_APPROVED
                    next_approver_role = "Supervisor"
                    
                    # Update leave with TL auto-approval
                    auto_approve_query = """
                        UPDATE "leaveApplication"
                        SET "leaveStatus" = %s,
                            "respondedByTeamLeader" = %s,
                            "respondedByTeamLeaderAt" = %s,
                            "updatedAt" = %s
                        WHERE id = %s
                    """
                    execute_query(auto_approve_query, [
                        auto_approved_status,
                        applicant_employee_id,  # Self-approved as TL
                        current_time,
                        current_time,
                        new_id
                    ])
                    print(f"[AUTO-APPROVE] Team Leader {applicant_employee_id} leave auto-approved through TL stage")

            # Handle certificate file upload
            certificate_path = None
            if certificate_file:
                certificate_path = save_leave_certificate(certificate_file, new_id)
                if certificate_path:
                    execute_query(
                        'UPDATE "leaveApplication" SET "leaveCertificate" = %s WHERE id = %s',
                        [certificate_path, new_id]
                    )

            # Schedule Celery task to check certificate upload at deadline (for sick leaves)
            if leave_type == LEAVE_TYPE_SICK and certificate_deadline and CELERY_AVAILABLE:
                celery_task_scheduled = False
                retry_count = 0
                max_retries = 3
                
                while not celery_task_scheduled and retry_count < max_retries:
                    try:
                        # Calculate countdown in seconds (how many seconds from now until deadline)
                        countdown_seconds = (certificate_deadline - datetime.now()).total_seconds()
                        
                        # Schedule the task to run after countdown_seconds
                        task_result = check_leave_certificate_deadline.apply_async(
                            args=[new_id],
                            countdown=countdown_seconds,
                            task_id=f"check_cert_{new_id}_{int(datetime.now().timestamp())}",
                            retry=True,
                            retry_policy={
                                'max_retries': 3,
                                'interval_start': 0,
                                'interval_step': 0.2,
                                'interval_max': 0.5,
                            }
                        )
                        
                        print(f"[CELERY] ✓ Scheduled certificate check task for leave {new_id}")
                        print(f"[CELERY] Task ID: {task_result.id} | Executes in {countdown_seconds/60:.2f} minutes")
                        celery_task_scheduled = True
                        
                    except Exception as celery_error:
                        retry_count += 1
                        error_type = type(celery_error).__name__
                        
                        # Classify error types
                        if 'Connection' in error_type or 'Timeout' in error_type:
                            print(f"[CELERY] ⚠ Redis connection error (attempt {retry_count}/{max_retries}): {str(celery_error)}")
                        elif 'Serialization' in error_type:
                            print(f"[CELERY] ✗ Serialization error - cannot retry: {str(celery_error)}")
                            break  # Don't retry serialization errors
                        else:
                            print(f"[CELERY] ✗ Unexpected error (attempt {retry_count}/{max_retries}): {error_type} - {str(celery_error)}")
                        
                        if retry_count < max_retries:
                            import time
                            time.sleep(1)  # Wait 1 second before retry
                        else:
                            print(f"[CELERY] ✗ FAILED to schedule task for leave {new_id} after {max_retries} attempts")
                            print(f"[CELERY] Leave created successfully but certificate check will NOT be automated")
                            traceback.print_exc()
                            
            elif leave_type == LEAVE_TYPE_SICK and not CELERY_AVAILABLE:
                print(f"[CELERY] ⚠ WARNING: Celery not available. Certificate check for leave {new_id} will not be scheduled.")

            # Send notification to next approver only for normal employees (not for auto-approved Team Leader/Supervisor leaves)
            # Skip notifications when Team Leader or Supervisor applies for their own leave
            if next_approver_role == "Team Leader":
                # Only send notification for normal employees (not Team Leader or Supervisor applying for themselves)
                try:
                    leave_type_names = {
                        LEAVE_TYPE_EMERGENCY: "Emergency",
                        LEAVE_TYPE_ANNUAL: "Annual",
                        LEAVE_TYPE_SICK: "Sick"
                    }
                    leave_type_name = leave_type_names.get(leave_type, "Leave")
                    employee_name = getattr(user, 'fullName', user.userName)
                    
                    # Normal employee - notify Team Leader
                    team_leader_query = """
                        SELECT tl.id AS "teamLeaderUserId", tl."fcmToken" AS "tlFcmToken", tl."fullName"
                        FROM "user" u
                        LEFT JOIN "user" tl ON u."teamLeaderId" = tl.id AND COALESCE(tl."isDeleted", '0') = '0'
                        WHERE u.id = %s
                    """
                    approver_result = execute_query(team_leader_query, [user.id], fetch='one')
                    
                    if approver_result:
                        if isinstance(approver_result, list):
                            approver_data = approver_result[0]
                        else:
                            approver_data = approver_result
                        
                        if approver_data.get('tlFcmToken'):
                            title = LEAVE_NOTIFICATION_NEW_REQUEST
                            body = f"{employee_name} has applied for {leave_type_name} leave from {start_date} to {end_date}. Awaiting your approval."
                            _send_notification(
                                recipient_user_id=approver_data.get('teamLeaderUserId'),
                                title=title,
                                body=body,
                                notification_type="NEW_LEAVE_REQUEST",
                                data_payload={"leaveId": str(new_id), "type": "NEW_LEAVE_REQUEST"},
                                fcm_token=approver_data.get('tlFcmToken'),
                                delay_seconds=60
                            )
                            print(f"[NOTIFICATION] Sent to Team Leader for leave {new_id}")
                
                except Exception as e:
                    print(f"ERROR: Failed to send leave notification: {str(e)}")
            else:
                # Team Leader or Supervisor applied - skip notification for auto-approved leaves
                print(f"[NOTIFICATION] Skipped notification for auto-approved leave {new_id} (applicant role: {next_approver_role})")

            # Log activity
            log_activity_raw(
                request=request,
                category='Leave',
                action='Apply',
                performer=user,
                details={
                    'leaveId': new_leave_id,
                    'leaveType': leave_type,
                    'startDate': start_date,
                    'endDate': end_date,
                    'totalDays': total_days
                }
            )

            return success_response(
                data={"id": new_id, "leaveId": new_leave_id},
                message=LEAVE_CREATED_SUCCESSFULLY,
                status_code=status.HTTP_201_CREATED
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"{ERROR_CREATING_LEAVE}: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def put(self, request, leave_id):
        """
        Update leave application.
        - Employees can upload certificate
        - Supervisors/Team Leaders can approve/reject
        """
        try:
            user = request.user
            data = getattr(request, 'data', None) or request.POST
            certificate_file = request.FILES.get('leaveCertificate') if hasattr(request, 'FILES') else None
            start_date = request.data.get('startDate')
            end_date = request.data.get('endDate')
            report_date = request.data.get('reportingDate')

            # Get user's groupNumber and isTeamLeader status
            user_role_query = """
                SELECT ur."groupNumber", ur."isTeamLeader"
                FROM "user" u
                JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE u.id = %s
            """
            user_role_result = execute_query(user_role_query, [user.id], many=False)
            user_group_number = user_role_result[0].get('groupNumber') if user_role_result else None
            is_user_team_leader = user_role_result[0].get('isTeamLeader') if user_role_result else False

            # Fetch existing leave application
            fetch_query = """
                SELECT la.*, u."reportingToId", u."teamLeaderId"
                FROM "leaveApplication" la
                JOIN "user" u ON la."employeeId" = u."employeeId" AND COALESCE(u."isDeleted", '0') = '0'
                WHERE la.id = %s AND COALESCE(la."isDeleted", 0) = 0
            """
            result = execute_query(fetch_query, [leave_id], fetch='one')
            
            if isinstance(result, list) and result:
                leave_record = result[0]
            else:
                leave_record = result
            
            if not leave_record:
                return error_response(
                    message=LEAVE_APPLICATION_NOT_FOUND,
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # Handle certificate upload (employee can upload)
            if certificate_file:
                certificate_path = save_leave_certificate(certificate_file, leave_id)
                if certificate_path:
                    execute_query(
                        'UPDATE "leaveApplication" SET "leaveCertificate" = %s, "updatedAt" = %s WHERE id = %s',
                        [certificate_path, datetime.now(), leave_id]
                    )
                    
                    log_activity_raw(
                        request=request,
                        category='Leave',
                        action='UploadCertificate',
                        performer=user,
                        details={'leaveId': leave_record.get('leaveId')}
                    )
                    
                    return success_response(message=CERTIFICATE_UPLOADED_SUCCESSFULLY)
            print("start_date:", start_date, "end_date:", end_date, "reporting_date:", report_date)
            if start_date and end_date and report_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                reporting_dt = datetime.strptime(report_date, "%Y-%m-%d")
                print("start_dt:", start_dt, "end_dt:", end_dt, "reporting_dt:", reporting_dt)

                if start_dt > end_dt:
                    return error_response(
                        message=END_DATE_BEFORE_START_DATE,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                
                if reporting_dt < end_dt:
                    return error_response(
                        message=REPORTING_DATE_BEFORE_END_DATE,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                
                if start_date and end_date:
                    total_days = (end_dt - start_dt).days + 1
                    execute_query(
                        'UPDATE "leaveApplication" SET "startDate" = %s, "endDate" = %s, "reportingDate" = %s, "totalDays" = %s, "updatedAt" = %s WHERE id = %s',
                        [start_date, end_date, report_date, total_days, datetime.now(), leave_id]
                    )
                    log_activity_raw(
                        request=request,
                        category='Leave',
                        action='UpdateDates',
                        performer=user,
                        details={
                            'leaveId': leave_record.get('leaveId'),
                            'newStartDate': start_date,
                            'newEndDate': end_date,
                            'newReportingDate': report_date
                        }
                    )

                    try:
                        # Get the leave applicant's details and role
                        employee_notify_query = """
                            SELECT u.id, u."fcmToken", u."fullName", u."teamLeaderId", u."reportingToId",
                                   ur."isTeamLeader", ur."roleOrderId"
                            FROM "user" u
                            JOIN "userrole" ur ON u."roleId" = ur."roleId"
                            WHERE u."employeeId" = %s AND COALESCE(u."isDeleted", '0') = '0'
                        """
                        emp_result = execute_query(employee_notify_query, [leave_record.get('employeeId')], fetch='one')
                        if isinstance(emp_result, list) and emp_result:
                            emp_data = emp_result[0]
                        else:
                            emp_data = emp_result

                        if emp_data:
                            employee_user_id = emp_data.get('id')
                            applicant_name = emp_data.get('fullName', 'Employee')
                            team_leader_id = emp_data.get('teamLeaderId')
                            reporting_to_id = emp_data.get('reportingToId')
                            applicant_is_team_leader = emp_data.get('isTeamLeader', False)
                            applicant_role_order_id = emp_data.get('roleOrderId')
                            applicant_is_supervisor = applicant_role_order_id == 3 and not applicant_is_team_leader

                            display_start = start_dt.strftime("%d-%m-%Y")
                            display_end = end_dt.strftime("%d-%m-%Y")

                            leave_type_name = get_leave_type_name(leave_record.get('leaveType'))
                            date_change_body = f"{applicant_name}'s {leave_type_name} leave dates updated to {display_start} to {display_end}"
                            self_leave = f"Your {leave_type_name} leave dates have been updated to {display_start} to {display_end}"

                            # Notify employee only if they are a regular employee (not TL, not Supervisor)
                            if not applicant_is_team_leader and not applicant_is_supervisor:
                                if employee_user_id != user.id:
                                    _send_notification(
                                        recipient_user_id=employee_user_id,
                                        title="Leave Dates Updated",
                                        body=self_leave,
                                        notification_type="LEAVE_DATES_UPDATED",
                                        data_payload={"leaveId": str(leave_id), "type": "LEAVE_DATES_UPDATED"},
                                        fcm_token=emp_data.get('fcmToken'),
                                        delay_seconds=60
                                    )

                            # Notify team leader only if applicant is a regular employee
                            if not applicant_is_team_leader and not applicant_is_supervisor:
                                if team_leader_id and team_leader_id != user.id:
                                    tl_query = """
                                        SELECT id, "fcmToken" FROM "user"
                                        WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
                                    """
                                    tl_result = execute_query(tl_query, [team_leader_id], fetch='one')
                                    tl_data = tl_result[0] if isinstance(tl_result, list) and tl_result else tl_result
                                    if tl_data:
                                        _send_notification(
                                            recipient_user_id=tl_data.get('id'),
                                            title="Leave Dates Updated",
                                            body=date_change_body,
                                            notification_type="LEAVE_DATES_UPDATED",
                                            data_payload={"leaveId": str(leave_id), "type": "LEAVE_DATES_UPDATED"},
                                            fcm_token=tl_data.get('fcmToken'),
                                            delay_seconds=60
                                        )

                            # Notify team leader (themselves) if applicant IS a team leader
                            if applicant_is_team_leader:
                                if employee_user_id != user.id:
                                    _send_notification(
                                        recipient_user_id=employee_user_id,
                                        title="Leave Dates Updated",
                                        body=self_leave,
                                        notification_type="LEAVE_DATES_UPDATED",
                                        data_payload={"leaveId": str(leave_id), "type": "LEAVE_DATES_UPDATED"},
                                        fcm_token=emp_data.get('fcmToken'),
                                        delay_seconds=60
                                    )

                            # Notify supervisor (themselves) if applicant IS a supervisor
                            if applicant_is_supervisor:
                                if employee_user_id != user.id:
                                    _send_notification(
                                        recipient_user_id=employee_user_id,
                                        title="Leave Dates Updated",
                                        body=self_leave,
                                        notification_type="LEAVE_DATES_UPDATED",
                                        data_payload={"leaveId": str(leave_id), "type": "LEAVE_DATES_UPDATED"},
                                        fcm_token=emp_data.get('fcmToken'),
                                        delay_seconds=60
                                    )

                            # Always notify the reporting supervisor for employee and TL leaves
                            if not applicant_is_supervisor and reporting_to_id and reporting_to_id != user.id:
                                sup_query = """
                                    SELECT id, "fcmToken" FROM "user"
                                    WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
                                """
                                sup_result = execute_query(sup_query, [reporting_to_id], fetch='one')
                                sup_data = sup_result[0] if isinstance(sup_result, list) and sup_result else sup_result
                                if sup_data:
                                    _send_notification(
                                        recipient_user_id=sup_data.get('id'),
                                        title="Leave Dates Updated",
                                        body=date_change_body,
                                        notification_type="LEAVE_DATES_UPDATED",
                                        data_payload={"leaveId": str(leave_id), "type": "LEAVE_DATES_UPDATED"},
                                        fcm_token=sup_data.get('fcmToken'),
                                        delay_seconds=60
                                    )
                    except Exception as notify_err:
                        print(f"ERROR: Failed to send date update notifications: {notify_err}")

                    if leave_record.get('leaveStatus') == LEAVE_STATUS_HR_APPROVED:
                        self._reconcile_moved_approved_leave(
                            leave_record.get('employeeId'),
                            leave_record.get('leaveType'),
                            leave_record.get('startDate'),
                            leave_record.get('endDate'),
                            start_date,
                            end_date
                        )

                    # If leaveStatus is also provided, don't return — fall through to handle it
                    if 'leaveStatus' not in data:
                        return success_response(message=LEAVE_DATES_UPDATED_SUCCESSFULLY, status_code=status.HTTP_200_OK)
            


                    # If leaveStatus is also provided, don't return — fall through to handle it
                    if 'leaveStatus' not in data:
                        return success_response(message=LEAVE_DATES_UPDATED_SUCCESSFULLY, status_code=status.HTTP_200_OK)
            



            # Handle approval/rejection (3-step approval: Team Leader → Supervisor → HR)
            # Frontend sends: leaveStatus=1 (Approve) or leaveStatus=2 (Reject)
            # Backend determines the actual stage based on current status
            if 'leaveStatus' in data:
                # Check permission based on groupNumber
                if user_group_number not in [ROLE_GROUP_SUPER_ADMIN, ROLE_GROUP_OFFICE_ADMIN, ROLE_GROUP_SUPERVISOR, ROLE_GROUP_TEAM_LEADER, ROLE_GROUP_HR] and not is_user_team_leader:
                    return error_response(
                        message=NO_PERMISSION_TO_APPROVE,
                        status_code=status.HTTP_403_FORBIDDEN
                    )
                
                current_status = leave_record.get('leaveStatus')
                action_type = int(data.get('leaveStatus'))  # 1=Approve, 2=Reject
                
                # Internal status values:
                # 0 = Pending (waiting for Team Leader)
                # 1 = Approved by Team Leader (waiting for Supervisor)
                # 2 = Approved by Supervisor (waiting for HR)
                # 3 = Approved by HR (Final approval)
                # 4 = Rejected (can be rejected at any stage)
                
                if action_type not in [LEAVE_ACTION_APPROVE, LEAVE_ACTION_REJECT]:
                    return error_response(
                        message=INVALID_LEAVE_ACTION,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                
                # Check if already finalized
                if current_status == LEAVE_STATUS_HR_APPROVED:
                    return error_response(
                        message=LEAVE_ALREADY_APPROVED,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                elif current_status == LEAVE_STATUS_REJECTED:
                    return error_response(
                        message=LEAVE_ALREADY_REJECTED,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                
                # Determine authorization based on current approval stage
                is_team_leader = user.id == leave_record.get('teamLeaderId') or is_user_team_leader
                is_supervisor = user.id == leave_record.get('reportingToId')
                is_hr_or_admin = user_group_number in [ROLE_GROUP_SUPER_ADMIN, ROLE_GROUP_OFFICE_ADMIN, ROLE_GROUP_HR]
                
                # Determine new status and authorization based on current stage
                new_status = None
                stage_name = None
                next_approver_role = None
                
                if current_status == LEAVE_STATUS_PENDING:
                    # Stage 1: Team Leader approval/rejection
                    if not is_team_leader and not is_hr_or_admin:
                        return error_response(
                            message=ONLY_TL_OR_HR_CAN_RESPOND,
                            status_code=status.HTTP_403_FORBIDDEN
                        )
                    if action_type == LEAVE_ACTION_APPROVE:
                        new_status = LEAVE_STATUS_TL_APPROVED
                        stage_name = "Team Leader"
                        next_approver_role = "Supervisor"
                    else:  # Reject
                        new_status = LEAVE_STATUS_REJECTED
                        stage_name = "Team Leader"
                
                elif current_status == LEAVE_STATUS_TL_APPROVED:
                    # Stage 2: Supervisor approval/rejection
                    if not is_supervisor and not is_hr_or_admin:
                        return error_response(
                            message=ONLY_SUPERVISOR_OR_HR_CAN_RESPOND,
                            status_code=status.HTTP_403_FORBIDDEN
                        )
                    if action_type == LEAVE_ACTION_APPROVE:
                        new_status = LEAVE_STATUS_SUPERVISOR_APPROVED
                        stage_name = "Supervisor"
                        next_approver_role = "HR"
                    else:  # Reject
                        new_status = LEAVE_STATUS_REJECTED
                        stage_name = "Supervisor"
                
                elif current_status == LEAVE_STATUS_SUPERVISOR_APPROVED:
                    # Stage 3: HR final approval/rejection
                    if not is_hr_or_admin:
                        return error_response(
                            message=ONLY_HR_CAN_RESPOND,
                            status_code=status.HTTP_403_FORBIDDEN
                        )
                    if action_type == LEAVE_ACTION_APPROVE:
                        new_status = LEAVE_STATUS_HR_APPROVED
                        stage_name = "HR"
                        next_approver_role = None  # No next approver
                    else:  # Reject
                        new_status = LEAVE_STATUS_REJECTED
                        stage_name = "HR"

                update_query = """
                    UPDATE "leaveApplication" 
                    SET "leaveStatus" = %s, 
                        "respondedByTeamLeader" = CASE WHEN %s = 1 THEN %s ELSE "respondedByTeamLeader" END,
                        "respondedByTeamLeaderAt" = CASE WHEN %s = 1 THEN %s ELSE "respondedByTeamLeaderAt" END,
                        "respondedBySupervisor" = CASE WHEN %s = 2 THEN %s ELSE "respondedBySupervisor" END,
                        "respondedBySupervisorAt" = CASE WHEN %s = 2 THEN %s ELSE "respondedBySupervisorAt" END,
                        "respondedByHR" = CASE WHEN %s = 3 THEN %s ELSE "respondedByHR" END,
                        "respondedByHRAt" = CASE WHEN %s = 3 THEN %s ELSE "respondedByHRAt" END,
                        "rejectedBy" = CASE WHEN %s = 4 THEN %s ELSE "rejectedBy" END,
                        "rejectedAt" = CASE WHEN %s = 4 THEN %s ELSE "rejectedAt" END,
                        "approvedBy" = %s,
                        "approvedAt" = %s,
                        "rejectionReason" = %s, 
                        "updatedAt" = %s
                    WHERE id = %s
                """
                
                current_time = datetime.now()
                employee_id = getattr(user, 'employeeId', None)
                rejection_reason = data.get('rejectionReason') if new_status == LEAVE_STATUS_REJECTED else None
                
                execute_query(
                    update_query,
                    [
                        new_status,
                        # Team Leader approval (status 1)
                        new_status, employee_id,
                        new_status, current_time,
                        # Supervisor approval (status 2)
                        new_status, employee_id,
                        new_status, current_time,
                        # HR approval (status 3)
                        new_status, employee_id,
                        new_status, current_time,
                        # Rejection (status 4)
                        new_status, employee_id,
                        new_status, current_time,
                        # General fields (kept for backward compatibility)
                        employee_id,
                        current_time,
                        rejection_reason,
                        current_time,
                        leave_id
                    ]
                )

                if new_status == LEAVE_STATUS_HR_APPROVED:
                    try:
                        sync_start_date = self._parse_leave_date(data.get('startDate') or leave_record.get('startDate'))
                        sync_end_date = self._parse_leave_date(data.get('endDate') or leave_record.get('endDate'))
                        self._reconcile_moved_approved_leave(
                            leave_record.get('employeeId'),
                            leave_record.get('leaveType'),
                            sync_start_date,
                            sync_end_date,
                            sync_start_date,
                            sync_end_date
                        )
                    except Exception as retro_err:
                        # Non-fatal: log but don't fail the approval
                        print(f"[LEAVE APPROVE] WARNING: Failed to retroactively update attendance: {retro_err}")

                # 3-step notification logic
                try:
                    if new_status in [LEAVE_STATUS_TL_APPROVED, LEAVE_STATUS_SUPERVISOR_APPROVED, LEAVE_STATUS_HR_APPROVED]:
                        if next_approver_role == "Supervisor":
                            # Team Leader approved, notify Supervisor
                            supervisor_query = """
                                SELECT u.id, u."fcmToken", u."fullName"
                                FROM "user" u
                                WHERE u.id = %s AND COALESCE(u."isDeleted", '0') = '0'
                            """
                            supervisor_result = execute_query(
                                supervisor_query, 
                                [leave_record.get('reportingToId')], 
                                fetch='one'
                            )
                            
                            if supervisor_result:
                                if isinstance(supervisor_result, list):
                                    supervisor_data = supervisor_result[0]
                                else:
                                    supervisor_data = supervisor_result
                                
                                if supervisor_data.get('fcmToken'):
                                    _send_notification(
                                        recipient_user_id=supervisor_data.get('id'),
                                        title=LEAVE_NOTIFICATION_PENDING_APPROVAL,
                                        body=f"Leave application ({leave_record.get('leaveId')}) is pending for your approval",
                                        notification_type="LEAVE_PENDING_SUPERVISOR",
                                        data_payload={
                                            "leaveId": str(leave_id), 
                                            "type": "LEAVE_PENDING_SUPERVISOR"
                                        },
                                        fcm_token=supervisor_data.get('fcmToken'),
                                        delay_seconds=60
                                    )
                        
                        elif next_approver_role == "HR":
                            # Supervisor approved, notify HR/Admin
                            hr_query = """
                                SELECT u.id, u."fcmToken", u."fullName"
                                FROM "user" u
                                INNER JOIN "userrole" ur ON u."roleId" = ur."roleId"
                                WHERE ur."groupNumber" IN (%s, %s) 
                                AND COALESCE(u."isDeleted", '0') = '0'
                            """
                            hr_results = execute_query(hr_query, [ROLE_GROUP_SUPER_ADMIN, ROLE_GROUP_OFFICE_ADMIN], fetch='all')
                            
                            if hr_results:
                                for hr_user in hr_results:
                                    if hr_user.get('fcmToken'):
                                        _send_notification(
                                            recipient_user_id=hr_user.get('id'),
                                            title=LEAVE_NOTIFICATION_PENDING_FINAL_APPROVAL,
                                            body=f"Leave application ({leave_record.get('leaveId')}) is pending final HR approval",
                                            notification_type="LEAVE_PENDING_HR",
                                            data_payload={
                                                "leaveId": str(leave_id), 
                                                "type": "LEAVE_PENDING_HR"
                                            },
                                            fcm_token=hr_user.get('fcmToken'),
                                            delay_seconds=60
                                        )
                        
                        elif next_approver_role is None and new_status == LEAVE_STATUS_HR_APPROVED:
                            # HR approved (final), notify employee
                            employee_query = """
                                SELECT u.id, u."fcmToken", u."fullName"
                                FROM "user" u
                                WHERE u."employeeId" = %s AND COALESCE(u."isDeleted", '0') = '0'
                            """
                            employee_result = execute_query(
                                employee_query, 
                                [leave_record.get('employeeId')], 
                                fetch='one'
                            )
                            
                            if employee_result:
                                if isinstance(employee_result, list):
                                    employee_data = employee_result[0]
                                else:
                                    employee_data = employee_result
                                
                                if employee_data.get('fcmToken'):
                                    _send_notification(
                                        recipient_user_id=employee_data.get('id'),
                                        title=LEAVE_NOTIFICATION_APPROVED,
                                        body=f"Your leave application has been approved by HR",
                                        notification_type="LEAVE_APPROVED",
                                        data_payload={
                                            "leaveId": str(leave_id), 
                                            "type": "LEAVE_APPROVED"
                                        },
                                        fcm_token=employee_data.get('fcmToken'),
                                        delay_seconds=60
                                    )
                    
                    elif new_status == LEAVE_STATUS_REJECTED:
                        # Notify employee about rejection
                        employee_query = """
                            SELECT u.id, u."fcmToken", u."fullName"
                            FROM "user" u
                            WHERE u."employeeId" = %s AND COALESCE(u."isDeleted", '0') = '0'
                        """
                        employee_result = execute_query(
                            employee_query, 
                            [leave_record.get('employeeId')], 
                            fetch='one'
                        )
                        
                        if employee_result:
                            if isinstance(employee_result, list):
                                employee_data = employee_result[0]
                            else:
                                employee_data = employee_result
                            
                            if employee_data.get('fcmToken'):
                                _send_notification(
                                    recipient_user_id=employee_data.get('id'),
                                    title=LEAVE_NOTIFICATION_REJECTED,
                                    body=f"Your leave application has been rejected by {stage_name}.",
                                    notification_type="LEAVE_REJECTED",
                                    data_payload={
                                        "leaveId": str(leave_id), 
                                        "type": "LEAVE_REJECTED"
                                    },
                                    fcm_token=employee_data.get('fcmToken'),
                                    delay_seconds=60
                                )
                
                except Exception as e:
                    print(f"ERROR: Failed to send leave status notification: {str(e)}")

                # Log activity with stage-specific actions
                if new_status == LEAVE_STATUS_REJECTED:
                    action = 'Reject'
                elif stage_name == "Team Leader":
                    action = 'Approve_TeamLeader'
                elif stage_name == "Supervisor":
                    action = 'Approve_Supervisor'
                elif stage_name == "HR":
                    action = 'Approve_HR'
                else:
                    action = 'Approve'
                
                log_activity_raw(
                    request=request,
                    category='Leave',
                    action=action,
                    performer=user,
                    details={
                        'leaveId': leave_record.get('leaveId'),
                        'leaveType': get_leave_type_name(leave_record.get('leaveType')),
                        'stage': stage_name,
                        'rejectionReason': rejection_reason if new_status == LEAVE_STATUS_REJECTED else None
                    }
                )

                # Determine success message based on status
                if new_status == LEAVE_STATUS_TL_APPROVED:
                    message = LEAVE_APPROVED_BY_TL_PENDING_SUPERVISOR
                elif new_status == LEAVE_STATUS_SUPERVISOR_APPROVED:
                    message = LEAVE_APPROVED_BY_SUPERVISOR_PENDING_HR
                elif new_status == LEAVE_STATUS_HR_APPROVED:
                    message = LEAVE_APPROVED_BY_HR_FINAL
                else:  # LEAVE_STATUS_REJECTED
                    message = LEAVE_REJECTED_BY_STAGE.format(stage=stage_name)
                
                return success_response(message=message)

            return error_response(
                message=NO_VALID_FIELDS_FOR_LEAVE_UPDATE,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"{ERROR_UPDATING_LEAVE}: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request):
        """
        Soft delete (cancel) leave applications.
        Only pending leaves can be cancelled and only by the applicant.
        """
        try:
            user = request.user
            leave_ids = request.data.get("leaveIds", [])
            
            if not leave_ids:
                return error_response(
                    message="No leave IDs provided",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            placeholders = ', '.join(['%s'] * len(leave_ids))
            
            # Verify ownership and status
            verify_query = f"""
                SELECT id, "leaveId", "employeeId", "leaveStatus"
                FROM "leaveApplication"
                WHERE id IN ({placeholders}) AND COALESCE("isDeleted", 0) = 0
            """
            leaves = execute_query(verify_query, leave_ids, many=True)
            
            if not isinstance(leaves, list):
                leaves = []

            for leave in leaves:
                # Only allow cancelling own pending leaves
                if leave.get('employeeId') != getattr(user, 'employeeId', None):
                    return error_response(
                        message="You can only cancel your own leave applications",
                        status_code=status.HTTP_403_FORBIDDEN
                    )
                
                if leave.get('leaveStatus') != 0:
                    return error_response(
                        message=f"Cannot cancel leave {leave.get('leaveId')}. Only pending leaves can be cancelled",
                        status_code=status.HTTP_400_BAD_REQUEST
                    )

            # Perform soft delete
            delete_query = f"""
                UPDATE "leaveApplication" 
                SET "isDeleted" = 1, "updatedAt" = %s 
                WHERE id IN ({placeholders})
            """
            execute_query(delete_query, [datetime.now()] + leave_ids)

            # Log activity
            for leave in leaves:
                log_activity_raw(
                    request=request,
                    category='Leave',
                    action='Cancel',
                    performer=user,
                    details={'leaveId': leave.get('leaveId')}
                )

            return success_response(
                message=f"{len(leave_ids)} leave application(s) cancelled successfully"
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error cancelling leave applications: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

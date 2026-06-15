from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from datetime import datetime
from django.utils import timezone
from api.utils import success_response, error_response, execute_query, log_activity_raw
from django.conf import settings
import json


class AttendanceListView(APIView):
    permission_classes = [IsAuthenticated]

    ATTENDANCE_STATUS_SORT_ORDER = """
        (CASE at."attendanceStatus"
            WHEN 'Normal' THEN 1
            WHEN 'Overtime' THEN 2
            WHEN 'HalfDay' THEN 3
            WHEN 'Absent' THEN 4
            ELSE 5
        END)
    """

    allowed_sort_columns = {
        "date": 'at."date"',
        "checkInTime": 'at."checkInTime"',
        "checkOutTime": 'at."checkOutTime"',
        "fullName": 'u."fullName"',
        "employeeId": 'u."employeeId"',
        "createdAt": 'at."createdAt"',
        "attendanceStatus": ATTENDANCE_STATUS_SORT_ORDER,
    }

    OVERTIME_STATUS_MAP = {
        'Pending': 1,
        'Approved': 2,
        'Rejected': 3,
        'TLApproved': 4,
        'NA': 0
    }

    def _build_voice_urls(self, request, voice_note_path_json):
        """Parse JSON voice paths and build absolute URLs."""
        if not voice_note_path_json:
            return []
        try:
            paths = json.loads(voice_note_path_json)
            return [request.build_absolute_uri(path) for path in paths]
        except (json.JSONDecodeError, TypeError):
            return []

    def get(self, request):
        is_export = request.query_params.get("isExport", "false").lower() == "true"
        start_date = request.query_params.get("startDate")
        end_date = request.query_params.get("endDate")

        if not start_date or not end_date:
            return error_response(message=START_DATE_AND_END_DATE_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

        try:
            page_size = int(request.query_params.get("pageSize", 10))
            page_number = int(request.query_params.get("page", 1))
            offset = (page_number - 1) * page_size

            search_query = request.query_params.get("search", "").strip()
            sort_param = request.query_params.get("sort", 'createdAt:desc').strip()
            
            # Original filter for reportingToId, kept for other roles if needed
            filter_by_reporting_to_id_param = int(request.query_params.get("filterByReportingToId", 0))

            attendance_status_filter = request.query_params.get("attendanceStatus", "")
            overtime_status_filter = request.query_params.get("overtimeStatus", "")
            emergency_checkin_filter = request.query_params.get("emergencyStatus", "")
            generate_report = request.query_params.get("generateReport", "false").lower() == "true"

            sort_column, sort_order = 'at."createdAt"', "desc"
            if sort_param:
                parts = sort_param.split(":")
                field = parts[0]
                if field in self.allowed_sort_columns:
                    sort_column = self.allowed_sort_columns[field]
                    if field in ["fullName", "employeeId"]:
                        sort_column = f"LOWER(TRIM({sort_column}))"
                    if len(parts) > 1 and parts[1].lower() in ["asc", "desc"]:
                        sort_order = parts[1].lower()

            if is_export:
                log_activity_raw(
                    request=request,
                    category='Export',
                    action='Attendance',
                    performer=request.user,
                    details={
                        'startDate': start_date,
                        'endDate': end_date,
                        'filtersUsed': dict(request.query_params)
                    }
                )


            filters = []
            filter_params = []
            
            # This variable will store a list of user IDs that the current user can see
            allowed_user_ids = []

            # --- START HIERARCHICAL REPORTING LOGIC ---
            # This handles multi-level reporting: Supervisor → Team Leader → Workers
            logged_in_user_id = request.user.id

            # Get the logged-in user's roleOrderId to check if they are a Supervisor
            # All Supervisors have roleOrderId = 3 (Garden, Pool, Technical Supervisors)
            user_role_query = """
                SELECT ur."roleOrderId", ur."isTeamLeader"
                FROM "user" u
                JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE u.id = %s
            """
            user_role_result = execute_query(user_role_query, [logged_in_user_id], many=False)
            logged_in_user_role_order_id = user_role_result[0]['roleOrderId'] if user_role_result else None
            is_team_leader = user_role_result[0].get('isTeamLeader', False) if user_role_result else False

            team_available = """
                SELECT * FROM "teamManagement" Where "teamLeaderId" = %s AND "isDeleted" = 0
            """
            team_available_result = execute_query(team_available, [logged_in_user_id], many=False)

            # If the logged-in user is a Supervisor (roleOrderId = 3)
            # Supervisors: Garden Supervisor, Pool Supervisor, Technical Supervisor
            if logged_in_user_role_order_id == 3:
                # Recursive query to get ALL subordinates in the hierarchy
                # This includes:
                # 1. Direct reports (Team Leaders and Workers)
                # 2. Indirect reports (Workers under Team Leaders)
                # 3. All levels below recursively
                recursive_subordinate_query = """
                    WITH RECURSIVE subordinates AS (
                        -- Start with users directly reporting to the supervisor
                        SELECT id, "reportingToId", "roleId"
                        FROM "user"
                        WHERE "reportingToId" = %s 
                          AND "isDeleted" = 0
                        
                        UNION ALL
                        
                        -- Recursively find users reporting to subordinates (e.g., Workers under Team Leaders)
                        SELECT u.id, u."reportingToId", u."roleId"
                        FROM "user" u
                        INNER JOIN subordinates s ON u."reportingToId" = s.id
                        WHERE u."isDeleted" = 0
                    )
                    SELECT DISTINCT id FROM subordinates;
                """
                subordinate_results = execute_query(recursive_subordinate_query, [logged_in_user_id], many=True)
                
                # Extract the IDs into a list
                allowed_user_ids = [res['id'] for res in subordinate_results]

                # If there are subordinates, add a filter for them
                if allowed_user_ids:
                    filters.append('at."labourUserId" IN %s')
                    filter_params.append(tuple(allowed_user_ids))
                else:
                    # If a supervisor has no subordinates, show no records
                    filters.append('1 = 0')

            elif logged_in_user_role_order_id == 4 or is_team_leader:
                # Team Leader logic with two modes:
                # A) If isTeamLeader=true: Show ONLY their own direct team members
                # B) Otherwise (roleOrderId=4 without flag): Show all users under same supervisor

                if is_team_leader and team_available_result:
                    # isTeamLeader=true: Only show users reporting directly to this team leader
                    recursive_subordinate_query = """
                        WITH RECURSIVE subordinates AS (
                            SELECT id, "reportingToId", "roleId"
                            FROM "user"
                            WHERE "teamLeaderId" = %s
                              AND "isDeleted" = 0
                            
                            UNION ALL
                            
                            SELECT u.id, u."reportingToId", u."roleId"
                            FROM "user" u
                            INNER JOIN subordinates s ON u."teamLeaderId" = s.id
                            WHERE u."isDeleted" = 0
                        )
                        SELECT DISTINCT id FROM subordinates;
                    """
                    subordinate_results = execute_query(recursive_subordinate_query, [logged_in_user_id], many=True)
                    allowed_user_ids = [res['id'] for res in subordinate_results]

                    if allowed_user_ids:
                        filters.append('at."labourUserId" IN %s')
                        filter_params.append(tuple(allowed_user_ids))
                    else:
                        filters.append('1 = 0')
                else:
                    # roleOrderId=4 without isTeamLeader flag:
                    # Show all users under the SAME SUPERVISOR
                    team_leader_supervisor_query = """
                        SELECT "reportingToId" FROM "user" WHERE id = %s
                    """
                    supervisor_result = execute_query(team_leader_supervisor_query, [logged_in_user_id], many=False)
                    
                    if supervisor_result and supervisor_result[0].get('reportingToId'):
                        supervisor_id = supervisor_result[0]['reportingToId']
                        
                        recursive_subordinate_query = """
                            WITH RECURSIVE subordinates AS (
                                SELECT id, "reportingToId", "roleId"
                                FROM "user"
                                WHERE "reportingToId" = %s
                                  AND "isDeleted" = 0
                                
                                UNION ALL
                                
                                SELECT u.id, u."reportingToId", u."roleId"
                                FROM "user" u
                                INNER JOIN subordinates s ON u."reportingToId" = s.id
                                WHERE u."isDeleted" = 0
                            )
                            SELECT DISTINCT id FROM subordinates;
                        """
                        subordinate_results = execute_query(recursive_subordinate_query, [supervisor_id], many=True)
                        allowed_user_ids = [res['id'] for res in subordinate_results]
                        
                        if logged_in_user_id in allowed_user_ids:
                            allowed_user_ids.remove(logged_in_user_id)
                        
                        if allowed_user_ids:
                            filters.append('at."labourUserId" IN %s')
                            filter_params.append(tuple(allowed_user_ids))
                        else:
                            filters.append('1 = 0')
                    else:
                        filters.append('1 = 0')

            elif filter_by_reporting_to_id_param and filter_by_reporting_to_id_param > 0:
                # This block is for Admins or other users explicitly filtering by a supervisor ID
                # Shows ALL users in the hierarchy including:
                # 1. Team Leaders directly reporting to the supervisor
                # 2. Workers directly reporting to the supervisor
                # 3. Workers reporting to those Team Leaders (recursive)
                recursive_subordinate_query = """
                    WITH RECURSIVE subordinates AS (
                        -- Start with users directly reporting to the specified supervisor
                        -- This includes BOTH Team Leaders AND direct Workers
                        SELECT id, "reportingToId", "roleId"
                        FROM "user"
                        WHERE "reportingToId" = %s
                          AND "isDeleted" = 0
                        
                        UNION ALL
                        
                        -- Recursively find users reporting to subordinates
                        -- This finds Workers reporting to Team Leaders
                        SELECT u.id, u."reportingToId", u."roleId"
                        FROM "user" u
                        INNER JOIN subordinates s ON u."reportingToId" = s.id
                        WHERE u."isDeleted" = 0
                    )
                    SELECT DISTINCT id FROM subordinates;
                """
                subordinate_results = execute_query(recursive_subordinate_query, [filter_by_reporting_to_id_param], many=True)
                allowed_user_ids = [res['id'] for res in subordinate_results]

                if allowed_user_ids:
                    filters.append('at."labourUserId" IN %s')
                    filter_params.append(tuple(allowed_user_ids))
                else:
                    # No subordinates found, show no records
                    filters.append('1 = 0')

            # --- END HIERARCHICAL REPORTING LOGIC ---  

            if search_query:
                filters.append('(u."fullName" ILIKE %s OR u."employeeId" ILIKE %s)')
                filter_params.extend([f'%{search_query}%', f'%{search_query}%'])

            # Process attendanceStatus (string filter)
            if attendance_status_filter:
                statuses = [s.strip() for s in attendance_status_filter.split(',') if s.strip()]
                if statuses:
                    filters.append('at."attendanceStatus" IN %s')
                    filter_params.append(tuple(statuses))

            # Process overtimeStatus (integer filter)
            if overtime_status_filter:
                status_strings = [s.strip() for s in overtime_status_filter.split(',') if s.strip()]
                status_integers = [self.OVERTIME_STATUS_MAP[s] for s in status_strings if s in self.OVERTIME_STATUS_MAP]
                if status_integers:
                    filters.append('at."overtimeStatus" IN %s')
                    filter_params.append(tuple(status_integers))

            # Process emergency check-in filter
            if emergency_checkin_filter:
                emergency_filter_value = emergency_checkin_filter.strip().lower()
                if emergency_filter_value == 'true':
                    # Show only records with emergency check-in
                    filters.append('at."emergencyCheckInTime" IS NOT NULL')
                elif emergency_filter_value == 'false':
                    # Show only records without emergency check-in
                    filters.append('at."emergencyCheckInTime" IS NULL')

            where_clause_additions = (" AND " + " AND ".join(filters)) if filters else ""

            base_params = [start_date, end_date] + filter_params

            # Note: The main queries (count_query and records_query) don't change much,
            # but the 'filters' and 'filter_params' now include the IDs from the recursive query.
            count_query = f"""
                SELECT COUNT(*) AS total FROM attendance at
                LEFT JOIN "user" u ON at."labourUserId" = u.id
                WHERE at."date" BETWEEN %s AND %s AND at."isDeleted" = 0 {where_clause_additions}
            """
            count_result = execute_query(count_query, base_params)
            total_count = 0
            total_count = count_result[0]['total'] if count_result else 0
            if isinstance(count_result, list) and count_result:
                if isinstance(count_result[0], dict) and 'total' in count_result[0]:
                    total_count = count_result[0]['total']


            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            # If the requested page > available pages, reset to Page 1
            if total_pages > 0 and page_number > total_pages:
                page_number = 1
            if total_pages == 0:
                page_number = 1

            # --- 3. CALCULATE OFFSET NOW ---
            offset = (page_number - 1) * page_size


            records_query = f"""
                SELECT
                    at.id AS "attendanceId", at.date, at."checkInTime", at."checkOutTime",
                    at."checkInLatitude", at."checkInLongitude", at."checkOutLatitude", at."checkOutLongitude",
                    at."calculatedRegularHours", at."overtimeHours", at."overtimeStatus", at."attendanceStatus", at."breakInTime", at."breakOutTime",at."earlyReason",
                    at."checkInDeviceId", at."checkOutDeviceId", at."earlyReasonStatus", at."emergencyCheckInTime", at."emergencyCheckInLatitude", at."emergencyCheckInLongitude",
                    at."emergencyCheckInDeviceId", at."emergencyCheckOutTime", at."emergencyCheckOutLatitude", at."emergencyCheckOutLongitude", at."emergencyCheckOutDeviceId",
                    at."emergencyHours", at."emergencyReason", at."emergencyCheckOutImages", at."emergencyCheckOutAudio",
                    u."fullName", u."employeeId", ot."reasonText" AS "overtimeReason",
                    ot."reasonPhotoPath", ot."reasonVoiceNotePath",
                    ot."tlReasonText", ot."tlReasonVoicePath",
                    ot."tlApprovedAt", ot."tlApprovedBy",
                    tl_user."fullName" AS "tlApprovedByName",
                    ot."supervisorApprovedAt", ot."supervisorApprovedBy",
                    sup_user."fullName" AS "supervisorApprovedByName"
                FROM attendance at
                LEFT JOIN "user" u ON at."labourUserId" = u.id
                LEFT JOIN "overtimeRequests" ot ON ot."attendanceRecordId" = at.id AND ot."isDeleted" = 0
                LEFT JOIN "user" tl_user ON ot."tlApprovedBy" = tl_user.id
                LEFT JOIN "user" sup_user ON ot."supervisorApprovedBy" = sup_user.id
                WHERE at."date" BETWEEN %s AND %s AND at."isDeleted" = 0 {where_clause_additions}
                ORDER BY {sort_column} {sort_order}
                
            """
            records_query_params = list(base_params)

            # Conditionally add pagination
            if not is_export:
                records_query += " LIMIT %s OFFSET %s"
                records_query_params.extend([page_size, offset])

            if generate_report:
                update_report_time_query = """
                    UPDATE public."reportsViews" 
                    SET "dateTime" = %s 
                    WHERE id = 1
                """
                # Note: Replace 'report_metadata' with your actual table name if different
                execute_query(update_report_time_query, [datetime.now()], many=False)

            # Execute the query with the potentially modified parameter list
            records = execute_query(records_query, records_query_params, many=True)

            response_data = []
            for record in records:
                total_hours = (float(record.get("calculatedRegularHours") or 0) +
                               float(record.get("overtimeHours") or 0))
                overtime_hours = float(record.get("overtimeHours") or 0)
                attendance_status = record["attendanceStatus"]

                photo_urls = []
                if record.get("reasonPhotoPath"):
                    try:
                        photo_paths = json.loads(record["reasonPhotoPath"])
                        photo_urls = [request.build_absolute_uri(path) for path in photo_paths]
                    except (json.JSONDecodeError, TypeError):
                        pass

                voice_urls = []
                if record.get("reasonVoiceNotePath"):
                    try:
                        voice_paths = json.loads(record["reasonVoiceNotePath"])
                        voice_urls = [request.build_absolute_uri(path) for path in voice_paths]
                    except (json.JSONDecodeError, TypeError):
                        pass


                emergency_photo_paths = []
                if record.get("emergencyCheckOutImages"):
                    try:
                        photo_paths = json.loads(record["emergencyCheckOutImages"])
                        emergency_photo_paths = [request.build_absolute_uri(settings.MEDIA_URL + path) for path in photo_paths]
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                emergency_voice_paths = []
                if record.get("emergencyCheckOutAudio"):
                    try:
                        voice_paths = json.loads(record["emergencyCheckOutAudio"])
                        emergency_voice_paths = [request.build_absolute_uri(settings.MEDIA_URL + path) for path in voice_paths]
                    except (json.JSONDecodeError, TypeError):
                        pass

                response_data.append({
                    "attendanceId": record["attendanceId"],
                    "date": record["date"].strftime("%Y-%m-%d"),
                    "checkInTime": record["checkInTime"].strftime("%Y-%m-%d %H:%M:%S") if record["checkInTime"] else None,
                    "checkOutTime": record["checkOutTime"].strftime("%Y-%m-%d %H:%M:%S") if record["checkOutTime"] else None,
                    "totalHours": round(total_hours, 2),
                    "fullName": record["fullName"],
                    "employeeId": record["employeeId"],
                    "checkInLat": record["checkInLatitude"],
                    "checkInLong": record["checkInLongitude"],
                    "checkOutLat": record["checkOutLatitude"],
                    "checkOutLong": record["checkOutLongitude"],
                    "breakInTime": record["breakInTime"].strftime("%Y-%m-%d %H:%M:%S") if record["breakInTime"] else None,
                    "breakOutTime": record["breakOutTime"].strftime("%Y-%m-%d %H:%M:%S") if record["breakOutTime"] else None,
                    "checkInDeviceId": record["checkInDeviceId"],
                    "checkOutDeviceId": record["checkOutDeviceId"],
                    "earlyReason": record["earlyReason"],
                    "earlyReasonStatus": record["earlyReasonStatus"],
                    "attendanceStatus": attendance_status,
                    "overtime": {
                        "overtimeStatus": record["overtimeStatus"],
                        "overtimeHours": overtime_hours,
                        "reason": record.get("overtimeReason") or "",
                        "imageUrls": photo_urls,
                        "audioUrls": voice_urls,
                        "tlApproval": {
                            "approvedByName": record.get("tlApprovedByName") or None,
                            "approvedTime": record["tlApprovedAt"].strftime("%Y-%m-%d %H:%M:%S") if record.get("tlApprovedAt") else None,
                            "reasonText": record.get("tlReasonText") or "",
                            "reasonVoiceUrls": self._build_voice_urls(request, record.get("tlReasonVoicePath"))
                        },
                        "supervisorApproval": {
                            "approvedByName": record.get("supervisorApprovedByName") or None,
                            "approvedTime": record["supervisorApprovedAt"].strftime("%Y-%m-%d %H:%M:%S") if record.get("supervisorApprovedAt") else None,
                        }
                    },
                    "emergencyStatus": True if record["emergencyCheckInTime"] else False,
                    "emergency":{
                        "emergencyCheckInTime": record["emergencyCheckInTime"].strftime("%Y-%m-%d %H:%M:%S") if record["emergencyCheckInTime"] else None,
                        "emergencyCheckInLatitude": record["emergencyCheckInLatitude"],
                        "emergencyCheckInLongitude": record["emergencyCheckInLongitude"],
                        "emergencyCheckInDeviceId": record["emergencyCheckInDeviceId"],
                        "emergencyCheckOutTime": record["emergencyCheckOutTime"].strftime("%Y-%m-%d %H:%M:%S") if record["emergencyCheckOutTime"] else None,
                        "emergencyCheckOutLatitude": record["emergencyCheckOutLatitude"],
                        "emergencyCheckOutLongitude": record["emergencyCheckOutLongitude"],
                        "emergencyCheckOutDeviceId": record["emergencyCheckOutDeviceId"],
                        "emergencyHours": record["emergencyHours"],
                        "emergencyReason": record["emergencyReason"],
                        "emergencyCheckOutImages": emergency_photo_paths,
                        "emergencyCheckOutAudio": emergency_voice_paths
                    }
                })

            # total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            return success_response(
                data={
                    "results": response_data,
                    "pagination": {
                        "totalRecords": total_count,
                        "totalPages": total_pages,
                        "currentPage": page_number,
                        "pageSize": page_size
                    }
                },
                message=ATTENDANCE_RECORDS_FETCHED_SUCCESSFULLY,
                status_code=status.HTTP_200_OK
            )
        except Exception as e:
            # logger.error(f"Error fetching attendance records: {e}", exc_info=True)
            return error_response(message=f"{UNEXPECTED_ERROR_OCCURRED}: {str(e)}",
                                   status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import (
    success_response,
    error_response,
    execute_query,
    send_mail_with_template_async,
    send_amc_completion_customer_push,
)
from api.messages import *
import datetime
import json
import pytz
from dateutil.relativedelta import relativedelta
from api.constants import (AMC_JOB_STATUS_NOT_STARTED, AMC_JOB_STATUS_IN_PROGRESS, AMC_JOB_STATUS_COMPLETED, AMC_STATUS_ACTIVE)


def _first_row(result):
    if isinstance(result, list):
        return result[0] if result else None
    return result


def _format_date_dd_mm_yyyy(value):
    if not value:
        return value

    if isinstance(value, datetime.datetime):
        return value.strftime('%d-%m-%Y')

    if isinstance(value, datetime.date):
        return value.strftime('%d-%m-%Y')

    if isinstance(value, str):
        for input_format in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                return datetime.datetime.strptime(value, input_format).strftime('%d-%m-%Y')
            except ValueError:
                continue

    return value



# For AMC Jobs List View

class AMCDailyJobsView(APIView):
    permission_classes = [IsAuthenticated]
    allowed_sort_fields = [
        "date", "amcJobName", "customerName", "gardenSupervisorName", "poolSupervisorName",
        "completionPercentage", "amcStatus", "duration"
    ]

    def get(self, request, amc_job_id=None):
        try:
            # --- 1. Parse Query Parameters ---
            start_date_str = request.query_params.get("startDate", "").strip()
            end_date_str = request.query_params.get("endDate", "").strip()
            search = request.query_params.get("search", "").strip()
            sort_param = request.query_params.get("sort", "").strip()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            customer_id = request.query_params.get("customerId", "").strip()
            garden_supervisor_id = request.query_params.get("gardenSupervisorId", "").strip()
            garden_team_leader_id = request.query_params.get("gardenTeamLeaderId", "").strip()
            pool_supervisor_id = request.query_params.get("poolSupervisorId", "").strip()
            pool_team_leader_id = request.query_params.get("poolTeamLeaderId", "").strip()
            supervisor_id = request.query_params.get("supervisorId", "").strip()
            team_leader_id = request.query_params.get("teamLeaderId", "").strip()
            employee_id = request.query_params.get("employeeId", "").strip()
            amc_job_status = request.query_params.get("amcJobStatus", "").strip()
            visit_type = request.query_params.get("visitType", "").strip()

            params = []

            # --- 2. Handle Dates ---
            if not amc_job_id:  # only apply dates if no jobId is passed
                if not start_date_str:
                    start_date = datetime.date.today()
                    end_date = datetime.date.today()
                else:
                    try:
                        start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                        end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else start_date
                    except ValueError:
                        return error_response(message=INVALID_DATE_FORMAT, status_code=status.HTTP_400_BAD_REQUEST)

                if start_date > end_date:
                    return error_response(message=START_DATE_AFTER_END_DATE, status_code=status.HTTP_400_BAD_REQUEST)

            # --- 3. Build the CTE ---
            cte = """
                WITH DailyVisitStats AS (
                    SELECT
                        aj."amcId",
                        aj."visitDate",
                        aj."amcJobId",
                        aj."visitStatus",
                        aj."visitType",
                        COALESCE(
                            CAST(
                                SUM(CASE WHEN vt.status = 2 THEN 1 ELSE 0 END) * 100.0
                                / NULLIF(COUNT(vt."visitTaskId"), 0)
                                AS INTEGER
                            ),
                            0
                        ) AS "completionPercentage"
                    FROM "AmcJobs" aj
                    JOIN "VisitTasks" vt ON aj."amcJobId" = vt."amcJobId"
            """

            if amc_job_id:
                cte += ' WHERE aj."amcJobId" = %s'
                params.append(int(amc_job_id))
            else:
                cte += ' WHERE aj."visitDate" BETWEEN %s AND %s'
                params.extend([start_date, end_date])

            cte += """
                    GROUP BY aj."amcId", aj."visitDate", aj."amcJobId", aj."visitStatus", aj."visitType"
                )
            """

            # --- 4. Main WHERE Clause ---
            where_clause = 'WHERE m."isDeleted" = 0 AND m."status" = 0'

            if search:
                where_clause += """ 
                    AND (m."amcJobName" ILIKE %s 
                         OR c."customerName" ILIKE %s 
                         OR COALESCE(su."fullName", su.name, su."userName") ILIKE %s)
                """
                search_param = f"%{search}%"
                params.extend([search_param] * 3)

            if customer_id:
                where_clause += ' AND m."customerId" = %s'
                params.append(customer_id)

            
            if supervisor_id:
                # Filter by supervisor considering visitType
                where_clause += ''' AND (
                    (dvs."visitType" = 1 AND m."gardenSupervisorId" = %s) OR
                    (dvs."visitType" = 2 AND m."poolSupervisorId" = %s) OR
                    (dvs."visitType" = 3 AND (m."gardenSupervisorId" = %s OR m."poolSupervisorId" = %s))
                )'''
                params.extend([supervisor_id, supervisor_id, supervisor_id, supervisor_id])

            if team_leader_id:
                # Filter by team leader considering visitType
                where_clause += ''' AND (
                    (dvs."visitType" = 1 AND m."gardenTeamLeaderId" = %s) OR
                    (dvs."visitType" = 2 AND m."poolTeamLeaderId" = %s) OR
                    (dvs."visitType" = 3 AND (m."gardenTeamLeaderId" = %s OR m."poolTeamLeaderId" = %s))
                )'''
                params.extend([team_leader_id, team_leader_id, team_leader_id, team_leader_id])

            if amc_job_status:
                where_clause += ' AND dvs."visitStatus" = %s'
                params.append(int(amc_job_status))

            if garden_supervisor_id:
                where_clause += ' AND m."gardenSupervisorId" = %s'
                params.append(garden_supervisor_id)
            if garden_team_leader_id:
                where_clause += ' AND m."gardenTeamLeaderId" = %s'
                params.append(garden_team_leader_id)
            if pool_supervisor_id:
                where_clause += ' AND m."poolSupervisorId" = %s'
                params.append(pool_supervisor_id)
            if pool_team_leader_id:
                where_clause += ' AND m."poolTeamLeaderId" = %s'
                params.append(pool_team_leader_id)
            if employee_id:
                # Get the employee's team leader employeeId if assigned to a team
                employee_team_leader_query = """
                    SELECT tl."employeeId" as "teamLeaderEmployeeId"
                    FROM "user" u
                    LEFT JOIN "user" tl ON u."teamLeaderId" = tl.id
                    WHERE u."employeeId" = %s AND u."isDeleted" = 0
                """
                employee_data = execute_query(employee_team_leader_query, [employee_id], many=True)
                employee_team_leader_id = employee_data[0].get('teamLeaderEmployeeId') if employee_data and employee_data[0] else None
                
                # Filter by employee considering visitType
                # Show jobs where employee is supervisor/team leader OR where employee's team leader is assigned
                where_clause += ''' AND (
                    (dvs."visitType" = 1 AND (m."gardenSupervisorId" = %s OR m."gardenTeamLeaderId" = %s'''
                params.extend([employee_id, employee_id])
                
                if employee_team_leader_id:
                    where_clause += ''' OR m."gardenTeamLeaderId" = %s'''
                    params.append(employee_team_leader_id)
                
                where_clause += ''')) OR
                    (dvs."visitType" = 2 AND (m."poolSupervisorId" = %s OR m."poolTeamLeaderId" = %s'''
                params.extend([employee_id, employee_id])
                
                if employee_team_leader_id:
                    where_clause += ''' OR m."poolTeamLeaderId" = %s'''
                    params.append(employee_team_leader_id)
                
                where_clause += ''')) OR
                    (dvs."visitType" = 3 AND (m."gardenSupervisorId" = %s OR m."gardenTeamLeaderId" = %s OR m."poolSupervisorId" = %s OR m."poolTeamLeaderId" = %s'''
                params.extend([employee_id, employee_id, employee_id, employee_id])
                
                if employee_team_leader_id:
                    where_clause += ''' OR m."gardenTeamLeaderId" = %s OR m."poolTeamLeaderId" = %s'''
                    params.extend([employee_team_leader_id, employee_team_leader_id])
                
                where_clause += '''))
                )'''
            if amc_job_id:
                where_clause += ' AND dvs."amcJobId" = %s'
                params.append(int(amc_job_id))

            if visit_type:
                where_clause += ' AND dvs."visitType" = %s'
                params.append(int(visit_type))

            # --- 5. Sorting ---
            order_by = 'ORDER BY dvs."visitDate" DESC, m."amcId" DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    sort_map = {
                        "date": 'dvs."visitDate"',
                        "amcJobName": 'm."amcJobName"',
                        "customerName": 'c."customerName"',
                        "gardenSupervisorName": 'COALESCE(su."fullName", su.name, su."userName")',
                        "poolSupervisorName": 'COALESCE(pu."fullName", pu.name, pu."userName")',
                        "completionPercentage": 'dvs."completionPercentage"',
                        "amcStatus": 'm."status"',
                        "duration": 'm."duration"',
                    }
                    order_by = f'ORDER BY {sort_map[sort_field]} {sort_direction}'

            # --- 6. Assemble Queries ---
            base_query = f"""
                FROM DailyVisitStats dvs
                JOIN "AMCMaster" m ON dvs."amcId" = m."amcId"
                LEFT JOIN "customer" c ON m."customerId" = c."customerId" AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "user" su ON m."gardenSupervisorId" = su."employeeId" AND COALESCE(su."isDeleted", 0) = 0
                LEFT JOIN "user" tl ON m."gardenTeamLeaderId" = tl."employeeId" AND COALESCE(tl."isDeleted", 0) = 0
                LEFT JOIN "user" pu ON m."poolSupervisorId" = pu."employeeId" AND COALESCE(pu."isDeleted", 0) = 0
                LEFT JOIN "user" pl ON m."poolTeamLeaderId" = pl."employeeId" AND COALESCE(pl."isDeleted", 0) = 0

                {where_clause}
            """

            count_query = cte + f"SELECT COUNT(*) AS total {base_query}"
            total_result = execute_query(count_query, list(params), fetch='one')

            total_count = total_result.get('total', 0) if isinstance(total_result, dict) else (
                total_result[0].get('total', 0) if isinstance(total_result, list) and total_result else 0
            )
            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 1

            query = cte + f"""
                SELECT
                    dvs."visitDate" AS "date",
                    m."amcJobName",
                    m."amcId",
                    m."jobId",
                    dvs."amcJobId",
                    dvs."visitType",
                    c."customerName",
                    COALESCE(su."fullName", su.name, su."userName") AS "gardenSupervisorName",
                    COALESCE(pu."fullName", pu.name, pu."userName") AS "poolSupervisorName",
                    m."gardenTeamLeaderId",
                    m."gardenSupervisorId",
                    m."poolSupervisorId",
                    m."poolTeamLeaderId",
                    COALESCE(tl."fullName", tl.name, tl."userName") AS "gardenTeamLeaderName",
                    COALESCE(pl."fullName", pl.name, pl."userName") AS "poolTeamLeaderName",
                    dvs."completionPercentage",
                    dvs."visitStatus" AS "amcJobStatus",
                    m.duration
                {base_query}
                {order_by}
            """

            if not is_export:
                query += ' LIMIT %s OFFSET %s'
                params.extend([page_size, (page - 1) * page_size])

            daily_jobs = execute_query(query, params, many=True)

            # --- CLEAN UP RESPONSE BASED ON VISIT TYPE ---
            # SQL already filters correctly, this just removes irrelevant fields from response
            for job in daily_jobs:
                visit_type_val = job.get('visitType')
                
                if visit_type_val == 1:  # Garden only - remove pool supervisor/team leader
                    job['poolSupervisorName'] = None
                    job['poolTeamLeaderName'] = None
                    job['poolSupervisorId'] = None
                    job['poolTeamLeaderId'] = None
                        
                elif visit_type_val == 2:  # Pool only - remove garden supervisor/team leader
                    job['gardenSupervisorName'] = None
                    job['gardenTeamLeaderName'] = None
                    job['gardenSupervisorId'] = None
                    job['gardenTeamLeaderId'] = None
                        
                # visitType=3 (Both) - keep all supervisor/team leader info

            response_data = {
                "results": daily_jobs,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                }
            }
            return success_response(data=response_data, message=DAILY_AMC_JOBS_FETCHED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=f"Error fetching daily AMC jobs: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AMCJobDetailsView(APIView):
    """
    API to get all task details for a specific visit, identified by its amc_job_id.
    This view has been updated to fetch issues from the consolidated 'taskManager' table
    and to calculate the contract due date and remaining days.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, amc_job_id=None):
        try:
            if not amc_job_id:
                return error_response(message=AMC_JOB_ID_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

            # --- CHANGES ARE IN THE FINAL SELECT STATEMENT OF THIS QUERY ---
            full_query_final = """
            WITH
                TargetVisit AS (
                    SELECT "amcId", "amcJobId", "visitDate", "visitStatus", "startTime", "endTime", "visitType", "startedBy", "endedBy", "isSupervisorFeedback", "isCustomerFeedback"
                    FROM "AmcJobs" WHERE "amcJobId" = %(amc_job_id)s
                ),
                AggregatedTasks AS (
                    -- This CTE for VisitTasks remains unchanged
                    SELECT
                        t."amcJobId",
                        jsonb_object_agg(
                            LOWER(SPLIT_PART(t.type, ' ', 1)),
                            t.tasks_array
                        ) AS tasks_data
                    FROM (
                        SELECT
                            vt."amcJobId", mt.type,
                            jsonb_agg(
                                jsonb_build_object(
                                    'visitTaskId', vt."visitTaskId",
                                    'taskName', mt.task,
                                    'status', vt.status,
                                    'completedAt', vt."completedAt",
                                    'updatedBy', vt."updatedBy",
                                    'category', mt.category
                                ) ORDER BY mt.task
                            ) AS tasks_array
                        FROM "VisitTasks" vt
                        JOIN "amcMaintenanceTasks" mt ON vt."maintenanceId" = mt."maintenanceId"
                        WHERE vt."amcJobId" = %(amc_job_id)s
                        GROUP BY vt."amcJobId", mt.type
                    ) t
                    GROUP BY t."amcJobId"
                ),
                AggregatedComments AS (
                    -- This CTE for Comments remains unchanged
                    SELECT
                        "amcJobId",
                        jsonb_build_object(
                            'comment', jc.comment,
                            'images', COALESCE(jc.images, '[]'::jsonb),
                            'audio', COALESCE(jc.audio, '[]'::jsonb),
                            'logDate', jc."logDate"::text
                        ) AS comments_data
                    FROM public."JobDayComments" jc
                    WHERE jc."amcJobId" = (SELECT "amcJobId" FROM TargetVisit LIMIT 1)
                    ORDER BY jc."logDate" DESC
                    LIMIT 1
                ),
                AggregatedIssues AS (
                    -- This CTE for Issues remains unchanged from your version
                    SELECT
                        tm."amcJobId",
                        jsonb_agg(
                            jsonb_build_object(
                                'issueId', tm.id,
                                'title', tm."taskName",
                                'status', tm."taskStatus",
                                'priority', tm."priority",
                                'supervisorId', tm."supervisorId",
                                'notes', COALESCE(tm."notes", '')::text,
                                'date', tm."startDate"::text,
                                'images', COALESCE(tm.images, '[]'::jsonb),
                                'audio', COALESCE(tm.audio, '[]'::jsonb)
                            ) ORDER BY tm."startDate" DESC
                        ) AS issues_data
                    FROM public."taskManager" tm
                    WHERE
                        tm."amcJobId" = (SELECT "amcJobId" FROM TargetVisit LIMIT 1)
                        AND tm."taskType" = 1
                        AND tm."isDeleted" = 0
                    GROUP BY tm."amcJobId"
                ),
                CompletionMetrics AS (
                    -- This CTE for completion percentage remains unchanged
                    SELECT
                        "amcJobId",
                        COALESCE(
                            CAST(SUM(CASE WHEN status = 2 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0) AS INTEGER),
                            0
                        ) AS "completionPercentage"
                    FROM "VisitTasks"
                    WHERE "amcJobId" = %(amc_job_id)s
                    GROUP BY "amcJobId"
                )
            SELECT
                jsonb_build_object(
                    'amcId', COALESCE(m."amcId", tv."amcId"),
                    'visitDate', tv."visitDate",
                    'amcJobStatus', tv."visitStatus",
                    'amcJobId', tv."amcJobId",
                    'visitType', tv."visitType",
                    'startTime', tv."startTime",
                    'endTime', tv."endTime",
                    'isSupervisorFeedback', tv."isSupervisorFeedback",
                    'isCustomerFeedback', tv."isCustomerFeedback",
                    'startDate', m."startDate",
                    'amcJobName', COALESCE(m."amcJobName", 'Unknown'),
                    'customerName', COALESCE(cust."customerName", 'Unknown'),
                    'gardenSupervisorName', COALESCE(sup."fullName", '-'),
                    'gardenTeamLeaderName', COALESCE(tl."fullName", '-'),
                    'poolSupervisorName', COALESCE(pu."fullName", '-'),
                    'poolTeamLeaderName', COALESCE(pl."fullName", '-'),
                    'gardenSupervisorId', COALESCE(m."gardenSupervisorId", ''),
                    'gardenTeamLeaderId', COALESCE(m."gardenTeamLeaderId", ''),
                    'poolSupervisorId', COALESCE(m."poolSupervisorId", ''),
                    'poolTeamLeaderId', COALESCE(m."poolTeamLeaderId", ''),
                    'completionPercentage', COALESCE(cm."completionPercentage", 0),
                    'issues', COALESCE(ai.issues_data, '[]'::jsonb),
                    'comments', COALESCE(ac.comments_data, '{}'::jsonb),
                    'duration', COALESCE(m.duration, 0),
                    'startedBy', tv."startedBy",
                    'startedByName', COALESCE(started_by."fullName", started_by.name, '-'),
                    'endedBy', tv."endedBy",
                    'endedByName', COALESCE(ended_by."fullName", ended_by.name, '-'),

                    -- ==================== MODIFIED SECTION START ====================
                    -- Calculate the last day of the month after adding the duration
                    'dueDate', (m."startDate" + (COALESCE(m.duration, 0) || ' months')::interval )::date,

                    -- Calculate the difference in days between the due date and today
                    'daysRemaining', ( (m."startDate" + (COALESCE(m.duration, 0) || ' months')::interval )::date - CURRENT_DATE ),
                    -- ===================== MODIFIED SECTION END =====================

                    'poolTask', COALESCE(at.tasks_data->'pool', '[]'::jsonb),
                    'gardenTask', COALESCE(at.tasks_data->'garden', '[]'::jsonb)
                ) AS job_object
            FROM TargetVisit tv
            INNER JOIN public."AMCMaster" m ON tv."amcId" = m."amcId" 
                AND COALESCE(m."isDeleted", 0) = 0 
                AND m."status" = 0   /* 0 = AMC_STATUS_ACTIVE */
            LEFT JOIN public.customer cust ON m."customerId" = cust."customerId"
            LEFT JOIN public.user sup ON m."gardenSupervisorId" = sup."employeeId"
            LEFT JOIN public.user tl ON m."gardenTeamLeaderId" = tl."employeeId"
            LEFT JOIN public.user pu ON m."poolSupervisorId" = pu."employeeId"
            LEFT JOIN public.user pl ON m."poolTeamLeaderId" = pl."employeeId"
            LEFT JOIN public.user started_by ON tv."startedBy" = started_by."employeeId"
            LEFT JOIN public.user ended_by ON tv."endedBy" = ended_by."employeeId"
            LEFT JOIN AggregatedTasks at ON tv."amcJobId" = at."amcJobId"
            LEFT JOIN AggregatedComments ac ON tv."amcJobId" = ac."amcJobId"
            LEFT JOIN AggregatedIssues ai ON tv."amcJobId" = ai."amcJobId"
            LEFT JOIN CompletionMetrics cm ON tv."amcJobId" = cm."amcJobId"
            """

            params = {'amc_job_id': amc_job_id}

            # --- EXECUTE QUERY AND PROCESS RESULTS (This part remains unchanged) ---
            result = execute_query(full_query_final, params, fetch=True, many=False)

            if isinstance(result, list):
                if not result:
                    return error_response(message=AMC_JOB_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)
                result = result[0]

            if not result or not result.get('job_object'):
                return error_response(message=AMC_JOB_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

            job_object = result.get('job_object')
            if isinstance(job_object, str):
                job_object = json.loads(job_object)

            # --- TRANSFORM MEDIA PATHS TO ABSOLUTE URLS (This part remains unchanged) ---
            # Process comments
            if job_object.get('comments') and isinstance(job_object['comments'], dict):
                image_paths = job_object['comments'].get('images', [])
                job_object['comments']['imageUrls'] = [request.build_absolute_uri(f"/media/{path}") for path in image_paths if path]
                if 'images' in job_object['comments']: del job_object['comments']['images']
                
                audio_paths = job_object['comments'].get('audio', [])
                job_object['comments']['audioUrls'] = [request.build_absolute_uri(f"/media/{path}") for path in audio_paths if path]
                if 'audio' in job_object['comments']: del job_object['comments']['audio']

            # Process issues
            if job_object.get('issues'):
                for issue in job_object['issues']:
                    image_paths = issue.get('images', [])
                    issue['imageUrls'] = [request.build_absolute_uri(f"/media/{path}") for path in image_paths if path]
                    if 'images' in issue: del issue['images']
                    
                    audio_paths = issue.get('audio', [])
                    issue['audioUrls'] = [request.build_absolute_uri(f"/media/{path}") for path in audio_paths if path]
                    if 'audio' in issue: del issue['audio']

            # --- NEW SECTION: CALCULATE TOTAL WORKING TIME ---
            total_working_time = None
            start_time_str = job_object.get('startTime')
            end_time_str = job_object.get('endTime')

            try:
                dubai_tz = pytz.timezone('Asia/Dubai')

                # Parse startTime only if present
                if start_time_str:
                    if start_time_str.endswith('Z'):
                        start_dt = datetime.datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                        start_dt = start_dt.astimezone(dubai_tz)
                    else:
                        start_dt = datetime.datetime.fromisoformat(start_time_str)
                        if start_dt.tzinfo is None:
                            start_dt = dubai_tz.localize(start_dt)
                else:
                    start_dt = None

                # Parse endTime only if present
                if end_time_str:
                    if end_time_str.endswith('Z'):
                        end_dt = datetime.datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                        end_dt = end_dt.astimezone(dubai_tz)
                    else:
                        end_dt = datetime.datetime.fromisoformat(end_time_str)
                        if end_dt.tzinfo is None:
                            end_dt = dubai_tz.localize(end_dt)
                else:
                    end_dt = None

                # Only calculate if BOTH are present
                if start_dt and end_dt:
                    delta = end_dt - start_dt
                    total_seconds = delta.total_seconds()
                    if total_seconds > 0:
                        total_minutes = int(total_seconds / 60)
                        hours = total_minutes // 60
                        minutes = total_minutes % 60
                        total_working_time = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                    else:
                        total_working_time = "0m"

            except Exception:
                total_working_time = None  # on parse error

            job_object['totalWorkingTime'] = total_working_time

            # --- FILTER SUPERVISOR/TEAM LEADER BASED ON VISIT TYPE ---
            visit_type = job_object.get('visitType')
            
            if visit_type == 1:  # Garden only - remove pool supervisor/team leader and pool tasks
                job_object['poolSupervisorName'] = None
                job_object['poolTeamLeaderName'] = None
                job_object['poolSupervisorId'] = None
                job_object['poolTeamLeaderId'] = None
                job_object['poolTask'] = []  # Clear pool tasks for garden-only jobs
            elif visit_type == 2:  # Pool only - remove garden supervisor/team leader and garden tasks
                job_object['gardenSupervisorName'] = None
                job_object['gardenTeamLeaderName'] = None
                job_object['gardenSupervisorId'] = None
                job_object['gardenTeamLeaderId'] = None
                job_object['gardenTask'] = []  # Clear garden tasks for pool-only jobs
            # visitType=3 (Both) - keep all supervisor/team leader info and all tasks


            return success_response(
                data=job_object,
                message=AMC_JOB_FETCHED_SUCCESSFULLY
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return error_response(
                message=f"Error fetching details for AMC Job ID {amc_job_id}: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class AMCJobStatusUpdateView(APIView):
    """
    API to update the status of a specific job in the AmcJobs table.
    Allowed status values: 0 (Not Started), 1 (In Progress), 2 (Completed).
    """
    permission_classes = [IsAuthenticated]

    def put(self, request, amc_job_id=None, **kwargs):
        try:
            # --- 1. VALIDATE PARAMETERS ---
            if not amc_job_id:
                return error_response(message=AMCJOBID_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

            data = request.data
            new_status = data.get('status')
            start_time = data.get('startTime')  # optional
            end_time = data.get('endTime')      # optional

            if new_status is None:
                return error_response(message=STATUS_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

            if not isinstance(new_status, int) or new_status not in [
                AMC_JOB_STATUS_NOT_STARTED,
                AMC_JOB_STATUS_IN_PROGRESS,
                AMC_JOB_STATUS_COMPLETED,
            ]:
                return error_response(message=STATUS_MUST_BE_INTEGER, status_code=status.HTTP_400_BAD_REQUEST)

            job_details_query = """
                SELECT
                    j."visitStatus" AS current_status,
                    j."visitDate"::text AS visit_date,
                    m."amcJobName",
                    m."customerId",
                    c."customerName",
                    c.email AS customer_email,
                    m."villaId",
                    COALESCE(
                        v."villaName",
                        CASE
                            WHEN m."villaId" IS NOT NULL THEN 'Villa-' || m."villaId"::text
                            ELSE 'Unknown Villa'
                        END
                    ) AS "villaName"
                FROM "AmcJobs" j
                INNER JOIN "AMCMaster" m ON j."amcId" = m."amcId"
                    AND COALESCE(m."isDeleted", 0) = 0
                LEFT JOIN "customer" c ON m."customerId" = c."customerId"
                    AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "villaDetails" v ON m."villaId" = v."id"
                    AND COALESCE(v."isDeleted", 0) = 0
                WHERE j."amcJobId" = %s
                LIMIT 1
            """
            job_details_result = execute_query(job_details_query, [amc_job_id], fetch='one')
            job_details_before_update = _first_row(job_details_result)

            if not job_details_before_update:
                return error_response(message=AMC_JOB_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

            current_status_value = job_details_before_update.get('current_status')
            try:
                current_status_value = int(current_status_value)
            except (TypeError, ValueError):
                current_status_value = None

            was_already_completed = current_status_value == AMC_JOB_STATUS_COMPLETED

            # Get the logged-in user's employeeId
            logged_in_user_id = getattr(request.user, 'employeeId', None)

            # --- 2. DYNAMIC UPDATE QUERY ---
            update_fields = ['"visitStatus" = %s']
            params = [new_status]

            if start_time is not None:
                update_fields.append('"startTime" = %s')
                params.append(start_time)
                # Track who started the job
                if logged_in_user_id:
                    update_fields.append('"startedBy" = %s')
                    params.append(logged_in_user_id)

            if end_time is not None:
                update_fields.append('"endTime" = %s')
                params.append(end_time)
                # Track who ended the job
                if logged_in_user_id:
                    update_fields.append('"endedBy" = %s')
                    params.append(logged_in_user_id)

            params.append(amc_job_id)

            update_query = f"""
            UPDATE "AmcJobs"
            SET {", ".join(update_fields)}
            WHERE "amcJobId" = %s
            RETURNING 
                "amcJobId",
                "amcId",
                "visitStatus",
                "visitDate"::text AS visit_date,
                "startTime"::text,
                "endTime"::text,
                "startedBy",
                "endedBy";
            """

            # --- 3. EXECUTE QUERY ---
            result = execute_query(update_query, params, fetch=True, many=False)

            if not result:
                return error_response(
                    message=AMC_JOB_NOT_FOUND,
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            job_details = result if isinstance(result, dict) else (
                result[0] if isinstance(result, list) and result else None
            )

            if not job_details:
                return error_response(
                    message=AMC_JOB_NOT_FOUND,
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            if new_status == AMC_JOB_STATUS_COMPLETED and not was_already_completed:
                customer_name = job_details_before_update.get('customerName') or 'Customer'
                amc_job_name = job_details_before_update.get('amcJobName') or 'AMC Job'
                villa_name = job_details_before_update.get('villaName') or 'Unknown Villa'
                visit_date = job_details_before_update.get('visit_date') or datetime.date.today().isoformat()
                formatted_visit_date = _format_date_dd_mm_yyyy(visit_date)
                customer_email = job_details_before_update.get('customer_email')
                customer_id = job_details_before_update.get('customerId')

                customer_context = {
                    "Customer Name": customer_name,
                    "date": formatted_visit_date,
                    "AMC Job Name": amc_job_name,
                    "Villa Name": villa_name,
                }

                try:
                    if customer_email:
                        send_mail_with_template_async(
                            "AMC Job Completion Customer",
                            customer_email,
                            customer_context,
                        )
                except Exception as email_error:
                    print(f"WARNING: Failed to send AMC completion email to customer for job {amc_job_id}: {email_error}")

                try:
                    send_amc_completion_customer_push(
                        customer_id=customer_id,
                        customer_name=customer_name,
                        amc_job_name=amc_job_name,
                        villa_name=villa_name,
                        visit_date=formatted_visit_date,
                    )
                except Exception as push_error:
                    print(f"WARNING: Failed to send AMC completion push to customer for job {amc_job_id}: {push_error}")

                try:
                    admin_email_result = execute_query(
                        'SELECT "officeAdminEmail" FROM "AdminSettings" WHERE COALESCE("isDeleted", 0) = 0 LIMIT 1',
                        fetch='one'
                    )
                    admin_email_data = _first_row(admin_email_result)
                    admin_email = admin_email_data.get('officeAdminEmail') if admin_email_data else None

                    if admin_email:
                        admin_context = {
                            "Customer Name": customer_name,
                            "AMC Job Name": amc_job_name,
                            "Villa Name": villa_name,
                            "date": formatted_visit_date,
                        }
                        send_mail_with_template_async(
                            "AMC Job Completion Admin",
                            admin_email,
                            admin_context,
                        )
                except Exception as email_error:
                    print(f"WARNING: Failed to send AMC completion email to admin for job {amc_job_id}: {email_error}")

            # --- 4. RETURN SUCCESS ---
            return success_response(
                data=job_details,
                message=AMC_JOB_UPDATED_SUCCESSFULLY
            )

        except Exception as e:
            return error_response(
                message=f"Error updating job status for amcJobId {amc_job_id}: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AmcJobTaskUpdateView(APIView):
    """
    API endpoint to update the status for a single task in the AmcJobTasks table.
    It automatically sets the 'completedAt' timestamp when the status is
    marked as completed.
    """
    permission_classes = [IsAuthenticated]

    def put(self, request, visit_task_id=None):
        """
        Handles updating a specific VisitTask's status.

        Expects a JSON body with only the 'status' field.
        Example Body:
        {
            "status": 2 // 0: Pending, 1: In-Progress, 2: Completed
        }
        """
        try:
            # --- 1. Validate Input ---
            if not visit_task_id:
                return error_response(message=VISIT_TASK_ID_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

            data = request.data
            new_status = data.get('status')
            updated_by = data.get('updatedBy')
            
            if new_status is None:
                return error_response(message=FIELD_STATUS_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)
            
            if updated_by is None:
                return error_response(message=FIELD_UPDATEDBY_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

            try:
                new_status = int(new_status)
                if new_status not in [0, 1]:
                    raise ValueError("Status must be 0 or 1.")
            except (ValueError, TypeError):
                return error_response(message=INVALID_STATUS_VALUE, status_code=status.HTTP_400_BAD_REQUEST)

            # --- 2. Determine `completedAt` Timestamp ---
            # If the new status is 2 (Completed), set the timestamp.
            # If the status is being changed back to Pending/In-Progress, clear the timestamp.
            completed_at_timestamp = None
            if new_status == 1:
                # Use timezone-aware datetime for PostgreSQL timestamptz
                completed_at_timestamp = datetime.datetime.now(datetime.timezone.utc)

            # --- 3. Construct and Execute the SQL UPDATE Query ---
            # This query is now simpler and more focused.
            update_query = """
                UPDATE "VisitTasks"
                SET
                    status = %s,
                    "completedAt" = %s,
                    "updatedBy" = %s
                WHERE "visitTaskId" = %s
                RETURNING "visitTaskId";
            """
            params = [new_status, completed_at_timestamp, updated_by, visit_task_id]

            result = execute_query(update_query, params, fetch='one')

            # --- 4. Handle Response ---
            if not result:
                return error_response(
                    message=TASK_NOT_FOUND,
                    status_code=status.HTTP_404_NOT_FOUND
                )

            return success_response(message=TASK_UPDATED_SUCCESSFULLY)

        except Exception as e:
            return error_response(
                message=f"An error occurred while updating the task: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )



class AMCProjectedCalendarView(APIView):
    """
    API to get a full, unpaginated, projected schedule of all jobs for active AMC contracts.
    
    Supports filtering and returns SEPARATE entries for garden and pool jobs with visitType.
    visitType: 1 = Garden, 2 = Pool
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # --- 1. Parse Query Parameters ---
            start_date_str = request.query_params.get("startDate", "").strip()
            end_date_str = request.query_params.get("endDate", "").strip()
            customer_id = request.query_params.get("customerId", "").strip()
            garden_supervisor_id = request.query_params.get("gardenSupervisorId", "").strip()
            pool_supervisor_id = request.query_params.get("poolSupervisorId", "").strip()
            villa_id = request.query_params.get("villaId", "").strip()
            visit_type_filter = request.query_params.get("visitType", "").strip()  # Optional filter

            # Default to the next 30 days if no date range is provided
            if not start_date_str:
                start_date = datetime.date.today()
                end_date = start_date + datetime.timedelta(days=30)
            else:
                try:
                    start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                    end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else start_date
                except ValueError:
                    return error_response(
                        message=INVALID_DATE_FORMAT,
                        status_code=status.HTTP_400_BAD_REQUEST
                    )

            if start_date > end_date:
                return error_response(
                    message=START_DATE_AFTER_END_DATE,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # --- 2. Fetch Filtered Master Contracts with Supervisor Names ---
            where_clauses = [f'm.status = {AMC_STATUS_ACTIVE}', 'COALESCE(m."isDeleted", 0) = 0']
            params = []

            if customer_id:
                where_clauses.append('m."customerId" = %s')
                params.append(customer_id)
            if garden_supervisor_id:
                where_clauses.append('m."gardenSupervisorId" = %s')
                params.append(garden_supervisor_id)
            if pool_supervisor_id:
                where_clauses.append('m."poolSupervisorId" = %s')
                params.append(pool_supervisor_id)
            if villa_id:
                where_clauses.append('m."villaId" = %s')
                params.append(villa_id)

            where_sql = " AND ".join(where_clauses)
            
            master_contracts_query = f"""
                SELECT 
                    m."amcId", m."amcJobName", m."startDate", m.duration, 
                    m."gardenVisitDays", m."poolVisitDays",
                    m."customerId", m."villaId",
                    c."customerName",
                    m."gardenSupervisorId",
                    COALESCE(su_g."fullName", su_g.name) AS "gardenSupervisorName",
                    m."poolSupervisorId",
                    COALESCE(su_p."fullName", su_p.name) AS "poolSupervisorName",
                    m."gardenTeamLeaderId",
                    COALESCE(tl_g."fullName", tl_g.name) AS "gardenTeamLeaderName",
                    m."poolTeamLeaderId",
                    COALESCE(tl_p."fullName", tl_p.name) AS "poolTeamLeaderName"
                FROM "AMCMaster" m
                LEFT JOIN "customer" c ON m."customerId" = c."customerId" AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "user" su_g ON m."gardenSupervisorId" = su_g."employeeId" AND COALESCE(su_g."isDeleted", 0) = 0
                LEFT JOIN "user" su_p ON m."poolSupervisorId" = su_p."employeeId" AND COALESCE(su_p."isDeleted", 0) = 0
                LEFT JOIN "user" tl_g ON m."gardenTeamLeaderId" = tl_g."employeeId" AND COALESCE(tl_g."isDeleted", 0) = 0
                LEFT JOIN "user" tl_p ON m."poolTeamLeaderId" = tl_p."employeeId" AND COALESCE(tl_p."isDeleted", 0) = 0
                WHERE {where_sql};
            """
            master_contracts = execute_query(master_contracts_query, params, many=True)

            # --- 3. Fetch Real Job IDs with visitType for Efficiency ---
            real_jobs_query = """
                SELECT 
                    j."amcId", 
                    j."amcJobId", 
                    j."visitDate",
                    j."visitType",
                    j."visitStatus"
                FROM "AmcJobs" j
                WHERE j."visitDate" BETWEEN %s AND %s
            """
            real_jobs_list = execute_query(real_jobs_query, [start_date, end_date], many=True)
            
            # Map: (amcId, visitDate, visitType) -> job details
            real_jobs_map = {
                (job['amcId'], job['visitDate'], job['visitType']): {
                    'amcJobId': job['amcJobId'],
                    'visitStatus': job['visitStatus']
                }
                for job in real_jobs_list
            }
            
            # Also track which dates have any real jobs (to show only actual jobs on those dates)
            dates_with_real_jobs = set(job['visitDate'] for job in real_jobs_list)

            # --- 4. Calculate the Full Schedule in Python ---
            full_schedule = []
            day_map = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6}

            for contract in master_contracts:
                contract_start = contract['startDate'].date() if isinstance(contract['startDate'], datetime.datetime) else contract['startDate']
                if not contract_start: continue

                duration = contract.get('duration', 0)
                contract_end = contract_start + relativedelta(months=duration)
                
                # Parse garden and pool visit days separately
                garden_days_json = contract.get('gardenVisitDays', [])
                pool_days_json = contract.get('poolVisitDays', [])
                
                if isinstance(garden_days_json, str):
                    try: garden_days_json = json.loads(garden_days_json)
                    except json.JSONDecodeError: garden_days_json = []
                
                if isinstance(pool_days_json, str):
                    try: pool_days_json = json.loads(pool_days_json)
                    except json.JSONDecodeError: pool_days_json = []
                
                garden_weekdays = {day_map[day.lower()] for day in garden_days_json if day.lower() in day_map}
                pool_weekdays = {day_map[day.lower()] for day in pool_days_json if day.lower() in day_map}
                
                # Calculate effective date range for this contract
                effective_start = max(start_date, contract_start)
                # Contract end is exclusive, so we need to check up to the day before
                # But only if contract_end is after start_date
                effective_end = min(end_date, contract_end - datetime.timedelta(days=1))
                
                # Skip if the effective date range is invalid
                if effective_start > effective_end:
                    continue

                current_date = effective_start
                while current_date <= effective_end:
                    weekday = current_date.weekday()
                    is_garden_day = weekday in garden_weekdays
                    is_pool_day = weekday in pool_weekdays
                    
                    # Check if this date has any real jobs created
                    date_has_jobs = current_date in dates_with_real_jobs
                    
                    # GARDEN job logic
                    if visit_type_filter == '' or visit_type_filter == '1':
                        real_job_garden = real_jobs_map.get((contract['amcId'], current_date, 1))
                        
                        # If date has real jobs, show ONLY if real job exists
                        # If date has no real jobs yet, show projected visits based on visit days
                        should_show_garden = real_job_garden if date_has_jobs else is_garden_day
                        
                        if should_show_garden:
                            full_schedule.append({
                                "amcId": contract['amcId'],
                                "amcJobId": real_job_garden['amcJobId'] if real_job_garden else None,
                                "amcJobName": contract['amcJobName'],
                                "date": current_date.strftime('%Y-%m-%d'),
                                "visitType": 1,
                                "visitTypeName": "Garden",
                                "visitStatus": real_job_garden['visitStatus'] if real_job_garden else 0,
                                "customerId": contract['customerId'],
                                "customerName": contract['customerName'],
                                "villaId": contract['villaId'],
                                "supervisorId": contract['gardenSupervisorId'],
                                "supervisorName": contract['gardenSupervisorName'],
                                "teamLeaderId": contract['gardenTeamLeaderId'],
                                "teamLeaderName": contract['gardenTeamLeaderName']
                            })
                    
                    # POOL job logic
                    if visit_type_filter == '' or visit_type_filter == '2':
                        real_job_pool = real_jobs_map.get((contract['amcId'], current_date, 2))
                        
                        # If date has real jobs, show ONLY if real job exists
                        # If date has no real jobs yet, show projected visits based on visit days
                        should_show_pool = real_job_pool if date_has_jobs else is_pool_day
                        
                        if should_show_pool:
                            full_schedule.append({
                                "amcId": contract['amcId'],
                                "amcJobId": real_job_pool['amcJobId'] if real_job_pool else None,
                                "amcJobName": contract['amcJobName'],
                                "date": current_date.strftime('%Y-%m-%d'),
                                "visitType": 2,
                                "visitTypeName": "Pool",
                                "visitStatus": real_job_pool['visitStatus'] if real_job_pool else 0,
                                "customerId": contract['customerId'],
                                "customerName": contract['customerName'],
                                "villaId": contract['villaId'],
                                "supervisorId": contract['poolSupervisorId'],
                                "supervisorName": contract['poolSupervisorName'],
                                "teamLeaderId": contract['poolTeamLeaderId'],
                                "teamLeaderName": contract['poolTeamLeaderName']
                            })
                    
                    current_date += datetime.timedelta(days=1)
            
            # --- 5. Assemble Final Response ---
            # Sort by date, then by visitType (garden first), then by job name
            full_schedule.sort(key=lambda x: (
                datetime.datetime.strptime(x['date'], '%Y-%m-%d'),
                x['visitType'],
                x['amcJobName'] or ''
            ))
            
            return success_response(
                data=full_schedule,
                message=AMC_JOBS_FETCHED_SUCCESSFULLY,
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(
                message=f"An error occurred: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

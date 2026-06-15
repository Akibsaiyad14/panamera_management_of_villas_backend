import json
import os
from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, save_amc_document, delete_amc_document, create_or_update_amc_schedules, generate_jobs_and_tasks_for_amc, log_activity_raw
from api.messages import *
from api.constants import *
from django.db import transaction
from psycopg2.errors import UniqueViolation
from django.db import IntegrityError
import datetime


def preprocess_json_field(field_value):
    """
    Convert input field to a list, handling strings and lists appropriately.
    """
    if isinstance(field_value, list):
        return field_value
    if isinstance(field_value, str):
        try:
            # Try parsing as JSON if it's a stringified list
            parsed = json.loads(field_value)
            if isinstance(parsed, list):
                return parsed
            # Handle comma-separated strings
            return [item.strip() for item in field_value.strip('[]').split(',') if item.strip()]
        except json.JSONDecodeError:
            # Handle plain comma-separated strings
            return [item.strip() for item in field_value.strip('[]').split(',') if item.strip()]
    return []


class AMCMasterView(APIView):
    permission_classes = [IsAuthenticated]
    allowed_sort_fields = [
        "amcId", "jobId", "amcJobName", "customerId", "startDate",
        "duration", "gardenVisitDays", "poolVisitDays", "supervisorId", "teamLeaderId", "status", "nocExpiryDate"
    ]

    def get(self, request, amc_id=None):
        """
        Handles fetching AMC jobs, now including NOC details + customerName/supervisorName/teamLeaderName.
        """
        try:
            if amc_id:
                # --- SINGLE AMC: JOIN customer and user (supervisor/team leader) to get names ---
                query = """
                    SELECT
                        m."amcId",
                        m."jobId",
                        m."amcJobName",
                        m."customerId",
                        c."customerName",
                        m."startDate",
                        m."duration",
                        m."gardenVisitDays",
                        m."poolVisitDays",
                        m."gardenSupervisorId",
                        COALESCE(su."fullName", su.name, su."userName") AS "gardenSupervisorName",
                        m."gardenTeamLeaderId",
                        COALESCE(tl."fullName", tl.name, tl."userName") AS "gardenTeamLeaderName",
                        m."poolSupervisorId",
                        COALESCE(psu."fullName", psu.name, psu."userName") AS "poolSupervisorName",
                        m."poolTeamLeaderId",
                        COALESCE(ptl."fullName", ptl.name, ptl."userName") AS "poolTeamLeaderName",

                        m."scopeOfWork",
                        m."additionalInfo",
                        m."status",
                        m."nocExpiryDate",
                        m."nocDocument"
                    FROM "AMCMaster" m
                    LEFT JOIN "customer" c
                        ON c."customerId" = m."customerId" AND COALESCE(c."isDeleted", 0) = 0
                    LEFT JOIN "user" su
                        ON su."employeeId" = m."gardenSupervisorId" AND COALESCE(su."isDeleted", 0) = 0
                    LEFT JOIN "user" psu
                        ON psu."employeeId" = m."poolSupervisorId" AND COALESCE(psu."isDeleted", 0) = 0
                    LEFT JOIN "user" ptl
                        ON ptl."employeeId" = m."poolTeamLeaderId" AND COALESCE(ptl."isDeleted", 0) = 0
                    LEFT JOIN "user" tl
                        ON tl."employeeId" = m."gardenTeamLeaderId" AND COALESCE(tl."isDeleted", 0) = 0
                    WHERE m."amcId" = %s AND m."isDeleted" = 0
                """
                amc_job = execute_query(query, [amc_id], fetch='one')
                if not amc_job:
                    return error_response(message=AMC_JOB_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

                # NOC document formatting (unchanged: single returns dict with url & folder)
                if amc_job.get('nocDocument'):
                    try:
                        doc_path = amc_job['nocDocument']
                        amc_job['nocDocument'] = request.build_absolute_uri(doc_path)
                            
                    except Exception:
                        amc_job['nocDocument'] = ''
                else:
                    amc_job['nocDocument'] = ''

                # Deserialize JSON fields
                if isinstance(amc_job.get('gardenVisitDays'), str):
                    try:
                        amc_job['gardenVisitDays'] = json.loads(amc_job['gardenVisitDays'])
                    except json.JSONDecodeError:
                        amc_job['gardenVisitDays'] = []
                if isinstance(amc_job.get('poolVisitDays'), str):
                    try:
                        amc_job['poolVisitDays'] = json.loads(amc_job['poolVisitDays'])
                    except json.JSONDecodeError:
                        amc_job['poolVisitDays'] = []
                if isinstance(amc_job.get('scopeOfWork'), str):
                    try:
                        amc_job['scopeOfWork'] = json.loads(amc_job['scopeOfWork'])
                    except json.JSONDecodeError:
                        amc_job['scopeOfWork'] = []

                return success_response(data=amc_job, message=AMC_JOB_FETCHED_SUCCESSFULLY)

            # -------- LIST / PAGINATED --------
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
            team_leader_id = request.query_params.get("teamLeaderId", "").strip()
            supervisor_id = request.query_params.get("supervisorId", "").strip()

            where_clause = 'WHERE m."isDeleted" = 0'
            params = []

            if search:
                where_clause += """ AND (
                    m."jobId" ILIKE %s OR
                    m."amcJobName" ILIKE %s OR
                    m."customerId" ILIKE %s OR
                    m."gardenSupervisorId" ILIKE %s OR
                    m."gardenTeamLeaderId" ILIKE %s OR
                    m."poolSupervisorId" ILIKE %s OR
                    m."poolTeamLeaderId" ILIKE %s
                )"""
                search_param = f"%{search}%"
                params.extend([search_param] * 7)

            if customer_id:
                where_clause += ' AND m."customerId" ILIKE %s'
                params.append(f"%{customer_id}%")
            if garden_supervisor_id:
                where_clause += ' AND m."gardenSupervisorId" ILIKE %s '
                params.append(f"%{garden_supervisor_id}%")
            if garden_team_leader_id:
                where_clause += ' AND m."gardenTeamLeaderId" ILIKE %s'
                params.append(f"%{garden_team_leader_id}%")
            if pool_supervisor_id:
                where_clause += ' AND m."poolSupervisorId" ILIKE %s '
                params.append(f"%{pool_supervisor_id}%")
            if pool_team_leader_id:
                where_clause += ' AND m."poolTeamLeaderId" ILIKE %s'
                params.append(f"%{pool_team_leader_id}%")
            if team_leader_id:
                where_clause += ' AND (m."gardenTeamLeaderId" ILIKE %s OR m."poolTeamLeaderId" ILIKE %s) '
                params.extend([f"%{team_leader_id}%", f"%{team_leader_id}%"])
            
            if supervisor_id:
                where_clause += ' AND (m."gardenSupervisorId" ILIKE %s OR m."poolSupervisorId" ILIKE %s) '
                params.extend([f"%{supervisor_id}%", f"%{supervisor_id}%"])

            order_by = 'ORDER BY m."amcId" DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    order_by = (
                        f'ORDER BY LOWER(TRIM(m."{sort_field}")) {sort_direction}'
                        if sort_field in ["amcJobName", "customerId"]
                        else f'ORDER BY m."{sort_field}" {sort_direction}'
                    )

            # COUNT (no change – still counts AMCMaster rows)
            count_query = f'SELECT COUNT(*) AS total FROM "AMCMaster" m {where_clause}'
            total_result = execute_query(count_query, list(params), fetch='one')
            # Make it robust whether dict or list-of-dicts is returned
            if isinstance(total_result, dict):
                total_count = total_result.get("total", 0)
            elif isinstance(total_result, list) and total_result:
                total_count = total_result[0].get("total", 0)
            else:
                total_count = 0

            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            # --- LIST SELECT: JOIN to pull names ---
            query = f"""
                SELECT
                    m."amcId",
                    m."jobId",
                    m."amcJobName",
                    m."customerId",
                    m."villaId",
                    c."customerName",
                    v."villaName",
                    m."startDate",
                    m."duration",
                    m."gardenVisitDays",
                    m."poolVisitDays",
                    m."gardenSupervisorId",
                    COALESCE(su."fullName", su.name, su."userName") AS "supervisorName",
                    m."gardenTeamLeaderId",
                    COALESCE(tl."fullName", tl.name, tl."userName") AS "teamLeaderName",
                    m."poolSupervisorId",
                    COALESCE(psu."fullName", psu.name, psu."userName") AS "poolSupervisorName",
                    m."poolTeamLeaderId",
                    COALESCE(ptl."fullName", ptl.name, ptl."userName") AS "poolTeamLeaderName",
                    m."scopeOfWork",
                    m."status",
                    m."additionalInfo",
                    m."nocExpiryDate",
                    m."nocDocument"
                FROM "AMCMaster" m
                LEFT JOIN "customer" c
                    ON c."customerId" = m."customerId" AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "user" su
                    ON su."employeeId" = m."gardenSupervisorId" AND COALESCE(su."isDeleted", 0) = 0
                LEFT JOIN "user" tl
                    ON tl."employeeId" = m."gardenTeamLeaderId" AND COALESCE(tl."isDeleted", 0) = 0
                LEFT JOIN "user" psu
                    ON psu."employeeId" = m."poolSupervisorId" AND COALESCE(psu."isDeleted", 0) = 0
                LEFT JOIN "user" ptl
                    ON ptl."employeeId" = m."poolTeamLeaderId" AND COALESCE(ptl."isDeleted", 0) = 0
                LEFT JOIN "villaDetails" v
                    ON v."id" = m."villaId" AND COALESCE(v."isDeleted", 0) = 0
                {where_clause}
                {order_by}
            """
            if not is_export:
                query += ' LIMIT %s OFFSET %s'
                params.extend([page_size, (page - 1) * page_size])

            amc_jobs = execute_query(query, params, many=True)

            # Post-process fields (NOC + JSON)
            for job in amc_jobs:
                # NOC doc for list: return a single absolute URL string (kept as in your code)
                if job.get('nocDocument'):
                    try:
                        job['nocDocument'] = request.build_absolute_uri(job['nocDocument'])
                    except Exception:
                        job['nocDocument'] = ''
                else:
                    job['nocDocument'] = ''

                # JSON fields
                if isinstance(job.get('gardenVisitDays'), str):
                    try:
                        job['gardenVisitDays'] = json.loads(job['gardenVisitDays'])
                    except json.JSONDecodeError:
                        job['gardenVisitDays'] = []
                if isinstance(job.get('poolVisitDays'), str):
                    try:
                        job['poolVisitDays'] = json.loads(job['poolVisitDays'])
                    except json.JSONDecodeError:
                        job['poolVisitDays'] = []
                if isinstance(job.get('scopeOfWork'), str):
                    try:
                        job['scopeOfWork'] = json.loads(job['scopeOfWork'])
                    except json.JSONDecodeError:
                        job['scopeOfWork'] = []

            response_data = {
                "results": amc_jobs,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                }
            }
            return success_response(data=response_data, message=AMC_JOBS_FETCHED_SUCCESSFULLY, status_code=status.HTTP_200_OK)

        except Exception as e:
                return error_response(message=f"{ERROR_FETCHING_AMC_JOBS} {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def post(self, request):
        """
        Creates a new AMC Job and automatically generates its recurring task schedules.
        """
        try:
            data = request.data
            job_id = data.get("jobId")
            
            # Validate required fields
            required_fields = ["jobId", "amcJobName", "startDate"]
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                return error_response(
                    message=f"{MISSING_REQUIRED_FIELDS_PREFIX} {', '.join(missing_fields)}",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            amc_start_date_str = data.get("startDate")
            try:
                # Handle both 'YYYY-MM-DD' and 'YYYY-MM-DDTHH:MM:SS.sss' formats
                # by taking only the part before the 'T'
                date_part = amc_start_date_str.split('T')[0]
                amc_start_date = datetime.datetime.strptime(date_part, '%Y-%m-%d').date()
            except (ValueError, TypeError, AttributeError): # Added AttributeError for safety if str is None
                return error_response(
                    message=INVALID_START_DATE_FORMAT,
                    status_code=status.HTTP_400_BAD_REQUEST
                )


            # Check for existing jobId (including deleted records to avoid unique constraint violation)
            check_query = 'SELECT "amcId" FROM "AMCMaster" WHERE "jobId" = %s'
            check_result = execute_query(check_query, [job_id], fetch='one')
            if check_result:
                return error_response(
                    message=JOB_ID_ALREADY_EXISTS,
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Handle NOC document
            noc_document_file = request.FILES.get('nocDocument')
            noc_document_path = save_amc_document(job_id, noc_document_file) if noc_document_file else None

            # Preprocess visitDays and scopeOfWork to ensure they are lists
            garden_visit_days = preprocess_json_field(data.get("gardenVisitDays", []))
            pool_visit_days = preprocess_json_field(data.get("poolVisitDays", []))
            scope_of_work = preprocess_json_field(data.get("scopeOfWork", []))


            noc_expiry_date = data.get("nocExpiryDate") or None

            customer_id = data.get("customerId")
            initial_status = AMC_STATUS_ACTIVE # Default to 0
            
            if customer_id:
                cust_query = 'SELECT "status" FROM "customer" WHERE "customerId" = %s'
                cust_res = execute_query(cust_query, [customer_id], fetch='one')
                
                if cust_res:
                    # This handles both cases: if cust_res is {'status':1} or if it's ({'status':1},)
                    res_data = cust_res if isinstance(cust_res, dict) else cust_res[0]
                    
                    # Get the status value from the dictionary
                    c_status = res_data.get('status') if isinstance(res_data, dict) else res_data
                    
                    # Final check against your defined constants
                    if int(c_status) in [STATUS_INACTIVE, STATUS_ON_HOLD]:
                        initial_status = AMC_STATUS_INACTIVE

            print(initial_status, "initial_status")
            # Use a transaction for data integrity
            with transaction.atomic():
                insert_query = """
                    INSERT INTO "AMCMaster"
                    ("jobId", "amcJobName", "customerId", "startDate", "duration", "gardenVisitDays", "poolVisitDays",
                    "gardenSupervisorId", "gardenTeamLeaderId", "scopeOfWork", "additionalInfo", "isDeleted", "status",
                    "nocExpiryDate", "nocDocument", "villaId", "poolSupervisorId", "poolTeamLeaderId")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s)
                    RETURNING "amcId"
                """
                params = [
                    job_id, data.get("amcJobName"), customer_id, data.get("startDate"),
                    data.get("duration"), json.dumps(garden_visit_days), json.dumps(pool_visit_days),
                    data.get("gardenSupervisorId"), data.get("gardenTeamLeaderId"), json.dumps(scope_of_work),
                    data.get("additionalInfo"), initial_status, noc_expiry_date,
                    noc_document_path, data.get("villaId"), data.get("poolSupervisorId"), data.get("poolTeamLeaderId")
                ]
                
                result = execute_query(insert_query, params, fetch='one')
                if not result:
                    return error_response(
                        message=FAILED_TO_CREATE_AMC_JOB,
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )

                # Handle result being a list or dictionary
                new_amc_id = result['amcId'] if isinstance(result, dict) else result[0]['amcId']

                generation_start_date = amc_start_date 
                # The end date is 30 days from the AMC's start date.
                generation_end_date = generation_start_date + datetime.timedelta(days=30)

                # Generate schedules and jobs for both garden and pool
                create_or_update_amc_schedules(new_amc_id, scope_of_work, garden_visit_days, pool_visit_days)
                generate_jobs_and_tasks_for_amc(new_amc_id, generation_start_date, generation_end_date)

            log_activity_raw(
                request,
                category='AMCJob',
                action='Add',
                performer=request.user,
                details={'id': job_id, 'title': data.get("amcJobName")}
            )

            return success_response(
                message=AMC_JOB_CREATED_SUCCESSFULLY,
                status_code=status.HTTP_201_CREATED
            )
        
        except (UniqueViolation, IntegrityError):
            return error_response(
                message=JOB_ID_ALREADY_EXISTS,
                status_code=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return error_response(
                message=f"Error creating AMC Job: {e}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )



    def put(self, request, amc_id):
        """
        Updates an existing AMC Job. This method now correctly orchestrates the entire update process:
        1. Cleans up any future, un-started jobs and tasks only if gardenVisitDays, poolVisitDays or scopeOfWork changed.
        2. Updates the master contract details.
        3. Regenerates the scheduling rules (AMCSchedule) only if necessary.
        4. Immediately regenerates the new schedule for the next 30 days only if necessary.
        """
        try:
            data = request.data
            # print("request data:", data)
            
            # --- Section A: Fetch and Validate Existing Data ---
            select_query = '''
                SELECT "jobId", "nocDocument", "amcJobName", "customerId", "startDate", "duration", "gardenVisitDays", "poolVisitDays",
                "gardenSupervisorId", "gardenTeamLeaderId", "scopeOfWork", "additionalInfo", "status",
                "nocExpiryDate", "villaId", "poolSupervisorId", "poolTeamLeaderId"
                FROM "AMCMaster" WHERE "amcId" = %s AND "isDeleted" = 0
            '''
            job_to_update = execute_query(select_query, [amc_id], fetch='one')
            if isinstance(job_to_update, list) and job_to_update:
                job_to_update = job_to_update[0]
            elif not job_to_update:
                return error_response(message=AMC_JOB_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

            # Preprocess request data
            job_id = job_to_update['jobId']
            old_document_path = job_to_update.get('nocDocument')
            final_document_path = old_document_path

            new_noc_file = request.FILES.get('nocDocument')
            if new_noc_file:
                final_document_path = save_amc_document(job_id, new_noc_file)
                if old_document_path:
                    delete_amc_document(old_document_path)

            # --- START: LOGIC FIX ---
            # Correctly compute new values, handling cases where a key is not sent vs. sent as empty
            new_amc_job_name = data.get("amcJobName", job_to_update["amcJobName"])
            new_customer_id = data.get("customerId", job_to_update["customerId"])
            new_start_date = data.get("startDate", job_to_update["startDate"])
            new_duration = data.get("duration", job_to_update["duration"])
            
            existing_garden_visit_days = json.loads(job_to_update["gardenVisitDays"] or '[]')
            existing_pool_visit_days = json.loads(job_to_update["poolVisitDays"] or '[]')
            existing_scope_of_work = json.loads(job_to_update["scopeOfWork"] or '[]')

            if "gardenVisitDays" in data:
                new_garden_visit_days = preprocess_json_field(data["gardenVisitDays"])
            else:
                new_garden_visit_days = existing_garden_visit_days

            if "poolVisitDays" in data:
                new_pool_visit_days = preprocess_json_field(data["poolVisitDays"])
            else:
                new_pool_visit_days = existing_pool_visit_days

            if "scopeOfWork" in data:
                new_scope_of_work = preprocess_json_field(data["scopeOfWork"])
            else:
                new_scope_of_work = existing_scope_of_work
            # --- END: LOGIC FIX ---

            new_garden_supervisor_id = data.get("gardenSupervisorId", job_to_update["gardenSupervisorId"])
            new_garden_team_leader_id = data.get("gardenTeamLeaderId", job_to_update["gardenTeamLeaderId"])
            new_pool_supervisor_id = data.get("poolSupervisorId", job_to_update["poolSupervisorId"])
            new_pool_team_leader_id = data.get("poolTeamLeaderId", job_to_update["poolTeamLeaderId"])
            new_additional_info = data.get("additionalInfo", job_to_update["additionalInfo"])
            
            new_status = data.get("status", job_to_update["status"])
            try:
                new_status = int(new_status) if new_status not in [None, ""] else job_to_update["status"]
            except (ValueError, TypeError):
                return error_response(message=INVALID_STATUS_VALUE_MUST_BE_INTEGER, status_code=status.HTTP_400_BAD_REQUEST)
            
            new_noc_expiry_date = data.get("nocExpiryDate", job_to_update["nocExpiryDate"])
            new_villa_id = data.get("villaId", job_to_update["villaId"])

            # Use a single, simple flag to determine if a full regeneration is needed.
            start_date_changed = str(new_start_date) != str(job_to_update["startDate"])
            duration_changed = int(new_duration) != int(job_to_update["duration"])
            garden_visit_days_changed = sorted(new_garden_visit_days) != sorted(existing_garden_visit_days)
            pool_visit_days_changed = sorted(new_pool_visit_days) != sorted(existing_pool_visit_days)
            scope_changed = sorted(new_scope_of_work) != sorted(existing_scope_of_work)
            
            regenerate_schedule = garden_visit_days_changed or pool_visit_days_changed or scope_changed or start_date_changed or duration_changed

            # --- Section B: The Atomic Update Operation (Restored Original Logic Flow) ---
            with transaction.atomic():
                # Step 1: **CRITICAL** - If the schedule needs changing, WIPE THE FUTURE CLEAN FIRST.
                if regenerate_schedule:
                    today = datetime.date.today()
                    
                    # Delete future, un-started tasks first due to foreign key constraints.
                    execute_query(
                        """
                            DELETE FROM "VisitTasks"
                            WHERE "amcJobId" IN (
                                SELECT "amcJobId" FROM "AmcJobs"
                                WHERE "amcId" = %s AND "visitDate" >= %s AND "visitStatus" = 0
                            );
                        """,
                        [amc_id, today]
                    )
                    # Then delete the future parent visit jobs.
                    execute_query(
                        'DELETE FROM "AmcJobs" WHERE "amcId" = %s AND "visitDate" >= %s AND "visitStatus" = 0',
                        [amc_id, today]
                    )

                # Step 2: Update the master contract. This is now the "source of truth" for the rebuild.
                update_query = """
                    UPDATE "AMCMaster" SET
                    "amcJobName" = %s, "customerId" = %s, "startDate" = %s, "duration" = %s, "gardenVisitDays" = %s, "poolVisitDays" = %s,
                    "gardenSupervisorId" = %s, "gardenTeamLeaderId" = %s, "scopeOfWork" = %s, "additionalInfo" = %s, "status" = %s,
                    "nocExpiryDate" = %s, "nocDocument" = %s, "villaId" = %s, "poolSupervisorId" = %s, "poolTeamLeaderId" = %s
                    WHERE "amcId" = %s;
                """
                params = [
                    new_amc_job_name, new_customer_id, new_start_date, new_duration, 
                    json.dumps(new_garden_visit_days), json.dumps(new_pool_visit_days),
                    new_garden_supervisor_id, new_garden_team_leader_id, json.dumps(new_scope_of_work), 
                    new_additional_info, new_status, new_noc_expiry_date, final_document_path, 
                    new_villa_id, new_pool_supervisor_id, new_pool_team_leader_id, amc_id
                ]
                execute_query(update_query, params)

                # Step 3 & 4: If needed, REBUILD the schedule and jobs from the newly updated "source of truth".
                if regenerate_schedule:
                    create_or_update_amc_schedules(amc_id, new_scope_of_work, new_garden_visit_days, new_pool_visit_days)
                    
                    today = datetime.date.today()
                    end_date = today + datetime.timedelta(days=30)
                    generate_jobs_and_tasks_for_amc(amc_id, today, end_date)

            log_activity_raw(
                request,
                category='AMCJob',
                action='Update',
                performer=request.user,
                details={'id': job_id, 'title': new_amc_job_name}
            )

            return success_response(message=AMC_JOB_UPDATED_SUCCESSFULLY, status_code=status.HTTP_200_OK)

        except Exception as e:
            # The transaction will automatically roll back on any exception.
            return error_response(message=f"Error updating AMC Job: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def delete(self, request, amc_id=None):
        """
        Soft deletes one or multiple AMC Jobs. File system is not affected.
        """
        try:
            ids_to_delete = []
            if amc_id:
                ids_to_delete.append(amc_id)
            else:
                amc_ids = request.data.get("amcIds")
                if not isinstance(amc_ids, list) or not all(isinstance(i, int) for i in amc_ids):
                    return error_response(message=AMC_JOB_IDS_MUST_BE_LIST, status_code=status.HTTP_400_BAD_REQUEST)
                ids_to_delete = amc_ids
            print(ids_to_delete)
            if not ids_to_delete:
                return error_response(message=NO_AMC_JOB_IDS_PROVIDED, status_code=status.HTTP_400_BAD_REQUEST)

            query = 'UPDATE "AMCMaster" SET "isDeleted" = 1 WHERE "amcId" = ANY(%s) AND "isDeleted" = 0 RETURNING "amcId"'
            result = execute_query(query, [ids_to_delete], many=True)

            deleted_count = len(result)

            amc_query = 'SELECT "amcJobName" FROM "AMCMaster" WHERE "amcId" = ANY(%s) AND "isDeleted" = 1'
            result1 = execute_query(amc_query, [ids_to_delete], many=True)

            if deleted_count == 0:
                return error_response(message=NO_MATCHING_ACTIVE_AMC_JOBS, status_code=status.HTTP_404_NOT_FOUND)

            for job in result1:
                log_activity_raw(
                    request,
                    category='AMCJob',
                    action='Delete',
                    performer=request.user,
                    details={'title': job['amcJobName']}
                )

            return success_response(message=AMC_JOBS_DELETED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=f"Error deleting AMC Job(s): {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

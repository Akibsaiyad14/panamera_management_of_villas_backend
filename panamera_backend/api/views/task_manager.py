from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
import json
from api.utils import execute_query, success_response, error_response, save_task_manager_files, log_activity_raw, format_date, to_int_or_none, _send_notification, _send_email, send_mail_with_template, format_datetime_value
from api.messages import *
from django.conf import settings
from django.utils import timezone
import traceback
from api.constants import *
import datetime
import pytz

import logging
logger = logging.getLogger(__name__)
#This was working properly code without customer supervisor and villa name from amc Issue

class TaskManagerView(APIView):
    """
    Handles consolidated CRUD for both Tasks (taskType=0) and Issues (taskType=1).
    Issues can be standalone or linked to a specific AMC Job visit via the 'amcJobId' field.
    """
    permission_classes = [IsAuthenticated]
    allowed_sort_fields = [
        "id", "taskName", "customerName", "villaName", "jobType", "priority",
        "supervisorName", "teamLeaderName", "startDate", "dueDate", "approvalStatus", "taskStatus",
        "amcJobId", "lastStatusDate"
    ]

    def _process_media_urls(self, request, media_json):
        """Helper to process JSONB media fields into full URLs."""
        if not media_json:
            return []
        try:
            paths = json.loads(media_json) if isinstance(media_json, str) else media_json
            return [request.build_absolute_uri(settings.MEDIA_URL + path) for path in paths]
        except (json.JSONDecodeError, TypeError):
            return []

    def _get_dubai_datetime(self, dt=None):
        """Convert datetime to Dubai timezone (UTC+4). If no datetime provided, returns current time in Dubai."""
        dubai_tz = pytz.timezone('Asia/Dubai')
        if dt is None:
            return timezone.now().astimezone(dubai_tz)
        # If dt is naive, assume it's in Dubai timezone, otherwise convert to Dubai
        if dt.tzinfo is None:
            return dubai_tz.localize(dt)
        return dt.astimezone(dubai_tz)

    def _get_close_date_value(self, task_status, close_date_input=None, existing_close_date=None):
        parsed_close_date = format_datetime_value(close_date_input)
        if close_date_input not in (None, '') and parsed_close_date is None:
            raise ValueError("Invalid value for lastStatusDate")

        # Ensure Dubai timezone for all lastStatusDate values
        if task_status == CLOSED:
            if parsed_close_date:
                return self._get_dubai_datetime(parsed_close_date)
            elif existing_close_date:
                return self._get_dubai_datetime(existing_close_date)
            else:
                return self._get_dubai_datetime()  # Current time in Dubai

        if close_date_input in (None, ''):
            return None

        # Return parsed_close_date in Dubai timezone
        if parsed_close_date:
            return self._get_dubai_datetime(parsed_close_date)
        return parsed_close_date

    def _get_customer_notification_target(self, customer_id):
        if not customer_id:
            return None

        query = '''
            SELECT id, "customerId", "fcmToken", "customerName"
            FROM public.customer
            WHERE "customerId" = %s AND COALESCE("isDeleted", 0) = 0
            LIMIT 1
        '''
        result = execute_query(query, [customer_id], fetch='one')
        if isinstance(result, list) and result:
            result = result[0]
        return result if isinstance(result, dict) else None

    def _get_users_by_group_numbers(self, group_numbers):
        if not group_numbers:
            return []

        placeholders = ", ".join(["%s"] * len(group_numbers))
        query = f'''
            SELECT u.id, u."fullName", u."fcmToken"
            FROM public."user" u
            INNER JOIN public."userrole" ur ON u."roleId" = ur."roleId"
            WHERE ur."groupNumber" IN ({placeholders})
              AND COALESCE(u."isDeleted", '0') = '0'
        '''
        result = execute_query(query, list(group_numbers), fetch='all', many=True)
        return result if isinstance(result, list) else []

    def _get_estimator_users(self):
        query = '''
            SELECT u.id, u."fullName", u."fcmToken"
            FROM public."user" u
            INNER JOIN public."userrole" ur ON u."roleId" = ur."roleId"
            WHERE ur."groupNumber" = %s
              AND COALESCE(u."isDeleted", '0') = '0'
        '''
        result = execute_query(query, [ESTIMATOR_GROUP_NUMBER], fetch='all', many=True)
        return result if isinstance(result, list) else []

    def _notify_customer(self, customer_id, title, body, notification_type, data_payload):
        customer = self._get_customer_notification_target(customer_id)
        if not customer:
            return
        try:
            _send_notification(
                recipient_customer_id=customer.get('customerId'),
                title=title,
                body=body,
                notification_type=notification_type,
                data_payload=data_payload,
                fcm_token=customer.get('fcmToken'),
                delay_seconds=60,
            )
        except Exception:
            pass

    def _notify_users(self, users, title, body, notification_type, data_payload):
        for user in users:
            try:
                _send_notification(
                    recipient_user_id=user.get('id'),
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    data_payload=data_payload,
                    fcm_token=user.get('fcmToken'),
                    delay_seconds=60,
                )
            except Exception:
                pass



    def get(self, request, task_id=None):
        """
        Fetches Tasks and/or Issues. Supports pagination by default, or all records if isExport=true.
        """
        try:
            base_select = """
                SELECT
                    t.id, t."taskName", t."customerId", c."customerName", t."villaId",
                    v."villaName", t."jobType", t."priority", t.reminder, t."supervisorId",
                    COALESCE(su."fullName", su.name) AS "supervisorName",
                    t."teamLeaderId", COALESCE(tl."fullName", tl.name) AS "teamLeaderName",
                    t."startDate", t."dueDate", t.notes, t."approvalStatus", t."taskType",
                    t."taskStatus", t."lastStatusDate", t.images, t."wasRequested", t."createdAt",
                    t."amcJobId", aj."amcJobName", t.audio, t."requestId"
                FROM "taskManager" t
                LEFT JOIN "customer" c ON t."customerId" = c."customerId" AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "villaDetails" v ON t."villaId" = v.id AND COALESCE(v."isDeleted", 0) = 0
                LEFT JOIN "user" su ON t."supervisorId" = su."employeeId" AND COALESCE(su."isDeleted", 0) = 0
                LEFT JOIN "user" tl ON t."teamLeaderId" = tl."employeeId" AND COALESCE(tl."isDeleted", 0) = 0
                LEFT JOIN "AmcJobs" aj ON t."amcJobId" = aj."amcJobId"
            """

            if task_id:
                # Fetch a single item (unchanged)
                query = f'{base_select} WHERE t.id = %s AND COALESCE(t."isDeleted", 0) = 0;'
                record_result = execute_query(query, [task_id], fetch='one')
                record = record_result[0] if isinstance(record_result, list) and record_result else record_result
                if not record:
                    return error_response(message=RECORD_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

                record['images'] = self._process_media_urls(request, record.get('images'))
                record['audio'] = self._process_media_urls(request, record.get('audio'))

                # --- NEW: fetch comments for this task ---
                comments_query = """
                    SELECT id, "attachmentType", path, "employeeId", "createdAt", "employeeName"
                    FROM "taskCommentsIssues"
                    WHERE "taskManagerId" = %s AND COALESCE("isDeleted", 0) = 0
                    ORDER BY "createdAt" ASC
                """
                comments = execute_query(comments_query, [record['id']], many=True)
                if not isinstance(comments, list):
                    comments = []
                normalized_comments = []
                for comment in comments:
                    processed = self._process_media_urls(request, comment.get('path'))
                    if isinstance(processed, list):
                        if len(processed) == 0:
                            comment['path'] = None
                        elif len(processed) == 1:
                            comment['path'] = processed[0]
                        else:
                            comment['path'] = processed
                    else:
                        comment['path'] = processed
                    normalized_comments.append(comment)

                # Always attach comments, even if empty
                record['comments'] = normalized_comments
                # --- END NEW ---
                return success_response(data=record, message=RECORD_FETCHED_SUCCESSFULLY)

            # --- Parse Query Parameters ---
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            search = request.query_params.get("search", "").strip()
            sort_param = request.query_params.get("sort", "").strip()
            customer_id = request.query_params.get("customerId", "").strip()
            supervisor_id = request.query_params.get("supervisorId", "").strip()
            task_type = request.query_params.get("taskType", "").strip()
            is_request = request.query_params.get("isRequest", "").lower() == "true"
            task_status = request.query_params.get("taskStatus", "").strip()
            approval_status = request.query_params.get("approvalStatus", "").strip()
            job_type = request.query_params.get("jobType", "").strip()
            priority = request.query_params.get("priority", "").strip()

            params = []
            where_conditions = ['COALESCE(t."isDeleted", 0) = 0']

            if is_request:
                where_conditions.append('t."wasRequested" = TRUE')
                
            if task_type in ["0", "1"]:  # match strings from query params
                where_conditions.append('t."taskType" = %s AND t."approvalStatus" = 1')
                params.append(task_type)
            elif task_type:  # for other types
                where_conditions.append('t."taskType" = %s')
                params.append(task_type)


            # --- Build WHERE clause with all filters ---
            if search:
                where_conditions.append("""
                    (
                        t."taskName" ILIKE %s OR
                        t."customerId" ILIKE %s OR
                        c."customerName" ILIKE %s OR
                        v."villaName" ILIKE %s OR
                        t."supervisorId" ILIKE %s OR
                        COALESCE(su."fullName", su.name) ILIKE %s
                        
                    )
                """)
                search_param = f"%{search}%"
                params.extend([search_param] * 6)

            if customer_id:
                where_conditions.append('t."customerId" ILIKE %s')
                params.append(f"%{customer_id}%")

            if supervisor_id:
                where_conditions.append('t."supervisorId" ILIKE %s')
                params.append(f"%{supervisor_id}%")

            # Convert task_status to int for proper comparison
            task_status_int = None
            if task_status:
                try:
                    task_status_int = int(task_status)
                except ValueError:
                    pass

            if task_status_int == ALL_EXCEPT_CLOSED:
                where_conditions.append('t."taskStatus" != %s')
                params.append(CLOSED)
            elif task_status_int is not None:
                where_conditions.append('t."taskStatus" = %s')
                params.append(task_status_int)
                
            if approval_status:
                where_conditions.append('t."approvalStatus" = %s')
                params.append(approval_status)

            if job_type:
                where_conditions.append('t."jobType" = %s')
                params.append(job_type)

            if priority:
                where_conditions.append('t."priority" = %s')
                params.append(priority)

            date_type = request.query_params.get("dateType")
            start_date = request.query_params.get("startDate")
            end_date = request.query_params.get("endDate")
            if date_type in ['startDate', 'dueDate', 'lastStatusDate'] and (start_date or end_date):
                db_column_name = f't."{date_type}"'
                if start_date and end_date:
                    where_conditions.append(f'{db_column_name} BETWEEN %s AND %s')
                    params.extend([start_date, end_date])
                elif start_date:
                    where_conditions.append(f'{db_column_name} >= %s')
                    params.append(start_date)
                elif end_date:
                    where_conditions.append(f'{db_column_name} <= %s')
                    params.append(end_date)

            where_clause = "WHERE " + " AND ".join(where_conditions)

            # --- Sorting Logic ---
            # Check if the logged-in user is a team leader and prioritize their tasks
            logged_in_employee_id = getattr(request.user, 'employeeId', None)
            team_leader_sort = ''
            if logged_in_employee_id:
                team_leader_sort = f'(CASE WHEN t."teamLeaderId" = \'{logged_in_employee_id}\' THEN 0 ELSE 1 END), '
            
            order_by = f'ORDER BY {team_leader_sort}t."id" DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    # Map sort field to database column
                    sort_column_mapping = {
                        "id": 't."id"',
                        "taskName": 't."taskName"',
                        "customerName": 'c."customerName"',
                        "villaName": 'v."villaName"',
                        "jobType": 't."jobType"',
                        "priority": 't."priority"',
                        "supervisorName": 'COALESCE(su."fullName", su.name)',
                        "teamLeaderName": 'COALESCE(tl."fullName", tl.name)',
                        "startDate": 't."startDate"',
                        "dueDate": 't."dueDate"',
                        "approvalStatus": 't."approvalStatus"',
                        "taskStatus": 't."taskStatus"',
                        "amcJobId": 't."amcJobId"',
                        "lastStatusDate": 't."lastStatusDate"'
                    }
                    # Apply LOWER(TRIM()) for string fields
                    if sort_field in ["taskName", "customerName", "villaName", "supervisorName", "teamLeaderName"]:
                        order_by = f'ORDER BY {team_leader_sort}LOWER(TRIM({sort_column_mapping[sort_field]})) {sort_direction}'
                    else:
                        order_by = f'ORDER BY {team_leader_sort}{sort_column_mapping[sort_field]} {sort_direction}'

            # --- Get Total Count ---
            base_from = 'FROM "taskManager" t LEFT JOIN "customer" c ON t."customerId" = c."customerId" LEFT JOIN "villaDetails" v ON t."villaId" = v.id LEFT JOIN "user" su ON t."supervisorId" = su."employeeId" AND COALESCE(su."isDeleted", 0) = 0'
            count_query = f'SELECT COUNT(t.id) AS total {base_from} {where_clause}'
            total_result = execute_query(count_query, list(params), fetch='one')
            
            total_count = 0
            if isinstance(total_result, list) and total_result:
                total_count = total_result[0].get("total", 0)
            elif isinstance(total_result, dict):
                total_count = total_result.get("total", 0)
            
            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 and not is_export else 1

            # --- Build Final Query ---
            query = f'{base_select} {where_clause} {order_by}'
            # print(query)

            # Conditionally add LIMIT and OFFSET for pagination
            if not is_export:
                query += ' LIMIT %s OFFSET %s'
                params.extend([page_size, (page - 1) * page_size])

            # Execute the final query
            records = execute_query(query, params, many=True)
            if not isinstance(records, list):
                records = []

            for record in records:
                record['images'] = self._process_media_urls(request, record.get('images'))
                record['audio'] = self._process_media_urls(request, record.get('audio'))

                # --- NEW: fetch comments for each record ---
                comments_query = """
                    SELECT id, "attachmentType", path, "employeeId", "createdAt", "employeeName"
                    FROM "taskCommentsIssues"
                    WHERE "taskManagerId" = %s AND COALESCE("isDeleted", 0) = 0
                    ORDER BY "createdAt" ASC
                """
                comments = execute_query(comments_query, [record['id']], many=True)
                if not isinstance(comments, list):
                    comments = []
                normalized_comments = []
                for comment in comments:
                    processed = self._process_media_urls(request, comment.get('path'))
                    if isinstance(processed, list):
                        if len(processed) == 0:
                            comment['path'] = None
                        elif len(processed) == 1:
                            comment['path'] = processed[0]
                        else:
                            comment['path'] = processed
                    else:
                        comment['path'] = processed
                    normalized_comments.append(comment)

                # Always attach comments, even if empty
                record['comments'] = normalized_comments

            # Build response
            response_data = {
                "results": records,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page if not is_export else 1,
                    "pageSize": page_size if not is_export else total_count,
                }
            }
            
            return success_response(data=response_data, message=RECORDS_FETCHED_SUCCESSFULLY)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error fetching records: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    # Make sure to import the _send_notification function if it's in another file
    # from .notifications import _send_notification

    def post(self, request):
        """
        Creates a new Task or Issue.
        - Handles original notification logic for internal issues.
        - Adds new notification logic for customer requests (`wasRequested` = true).
        - Correctly looks up supervisorId from villaId for customer requests.
        """
        try:
            creating_user = request.user
            data = request.POST
            image_files = request.FILES.getlist('images')
            audio_files = request.FILES.getlist('audio')

            required_fields = ["taskName", "taskType"]
            if not all(field in data for field in required_fields):
                return error_response(message=TASK_REQUIRED_FIELDS, status_code=status.HTTP_400_BAD_REQUEST)
            

            now = datetime.datetime.now()
            year_short = now.strftime('%y')
            month = now.strftime('%m')
            prefix = f"R{year_short}{month}-"


            last_id_query = """
                SELECT "requestId"
                FROM "taskManager"
                WHERE "requestId" LIKE %s
                
                ORDER BY CAST(SPLIT_PART("requestId", '-', 2) AS INTEGER) DESC
                LIMIT 1;
            """
            last_id_result = execute_query(last_id_query, [f"{prefix}%"], fetch='one')

            # --- CORRECTED LOGIC ---
            # Check if the returned list is not empty
            if last_id_result:
                # Get the dictionary from the first element of the list
                last_record_dict = last_id_result[0] 
                
                # Now, safely check the dictionary for the key
                if last_record_dict.get('requestId'):
                    last_id_str = last_record_dict['requestId']
                    last_seq_num = int(last_id_str.split('-')[-1])
                    new_seq_num = last_seq_num + 1
                else:
                    # This case is unlikely if the query is correct, but it's safe to have
                    new_seq_num = 1 
            else:
                # The list was empty, meaning no tasks exist for this month yet. Start the sequence at 1.
                new_seq_num = 1 
            # --- END OF CORRECTION ---

            new_request_id = f"{prefix}{new_seq_num:02d}"
            # print("Generated new_request_id:", new_request_id)

            # --- Data Extraction ---
            amcJobId = to_int_or_none(data.get('amcJobId'))
            customerId = data.get('customerId')
            villaId = to_int_or_none(data.get('villaId'))
            supervisorId = data.get('supervisorId')
            # Track if a supervisor was explicitly provided by the request payload.
            provided_supervisor_id = supervisorId if supervisorId not in (None, '', 'null') else None
            supervisorId = provided_supervisor_id
            # print("supervisorId", supervisorId)
            was_requested = data.get('wasRequested', 'false').lower() == 'true'

            result_from_amc = None
            if amcJobId:
                fetch_query = """
                    SELECT am."customerId", am."gardenSupervisorId", am."poolSupervisorId", am."villaId", am."amcJobName"
                    FROM "AMCMaster" am JOIN "AmcJobs" aj ON am."amcId" = aj."amcId"
                    WHERE aj."amcJobId" = %s
                """
                result_from_amc = execute_query(fetch_query, [amcJobId], fetch=True)
                if result_from_amc:
                    result_from_amc = result_from_amc[0]
                    customerId = result_from_amc.get('customerId')
                    # print(customerId)
                    supervisorId = supervisorId
                    villaId = result_from_amc.get('villaId')
                else:
                    return error_response(message="AMC Job not found", status_code=status.HTTP_400_BAD_REQUEST)

            # =================================================================
            ### NEW: SUPERVISOR LOOKUP FOR CUSTOMER REQUESTS ###
            # =================================================================
            # If it's a customer request and we don't have a supervisor yet, find one using the villaId.
            if was_requested and not supervisorId and villaId:
                # Do not auto-assign AMC supervisors for customer requests.
                print(f"Customer request for villaId {villaId} will not auto-assign an AMC supervisor.")
            
            # --- Database INSERT (Now uses the correctly found supervisorId) ---
            task_type = to_int_or_none(data.get('taskType')) or 0
            job_type = to_int_or_none(data.get('jobType')) or 0
            task_status = to_int_or_none(data.get('taskStatus')) or 0
            try:
                close_date = self._get_close_date_value(task_status, data.get('lastStatusDate'))
            except ValueError as exc:
                return error_response(message=str(exc), status_code=status.HTTP_400_BAD_REQUEST)

            teamLeaderId = data.get('teamLeaderId')
            
            insert_query = """
                INSERT INTO "taskManager" ("requestId", "taskName", "customerId", "villaId", "jobType", "priority", reminder, "supervisorId", "teamLeaderId", "startDate", "dueDate", "lastStatusDate", notes, "approvalStatus", "taskType", "taskStatus", "wasRequested", "amcJobId", images, audio) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """
            params = [
                new_request_id, data.get('taskName'), customerId, villaId, job_type, to_int_or_none(data.get('priority')), data.get('reminder'),
                supervisorId, teamLeaderId, format_date(data.get('startDate')), format_date(data.get('dueDate')), close_date, data.get('notes'),
                to_int_or_none(data.get('approvalStatus')) or 0, task_type, task_status,
                was_requested, amcJobId, json.dumps([]), json.dumps([])
            ]
            new_record_result = execute_query(insert_query, params, fetch=True)
            
            new_id = new_record_result[0].get("id") if new_record_result else None
            if not new_id:
                return error_response(message=FAILED_CREATE_AND_RETRIEVE_ID, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            image_paths = save_task_manager_files(image_files, new_id, 'images') if image_files else []
            audio_paths = save_task_manager_files(audio_files, new_id, 'audio') if audio_files else []
            if image_paths or audio_paths:
                execute_query('UPDATE "taskManager" SET images = %s, audio = %s WHERE id = %s;', [json.dumps(image_paths), json.dumps(audio_paths), new_id])

            # =================================================================
            ### NOTIFICATION LOGIC ###
            # =================================================================
            task_name = data.get('taskName')

            if was_requested:
                # New Customer Request notification logic
                print(f"Task ID {new_id} is a customer request. Initiating request notifications.")
                if supervisorId and customerId and provided_supervisor_id:
                    supervisor_query = 'SELECT id, "fcmToken", "fullName", "reportingToId" FROM "user" WHERE "employeeId" = %s'
                    supervisor_result = execute_query(supervisor_query, [supervisorId], fetch=True)
                    customer_result = execute_query('SELECT email, "customerName" FROM customer WHERE "customerId" = %s', [customerId], fetch=True)
                    email_config_result = execute_query('SELECT "emailId", "appPassword", "smtpServer", "smtpPort" FROM "emailSettings" WHERE COALESCE("isDeleted", 0) = 0 LIMIT 1', fetch=True)

                    supervisor_data = supervisor_result[0] if supervisor_result else None
                    customer_data = customer_result[0] if customer_result else None
                    email_config = email_config_result[0] if email_config_result else None
                    task_name = data.get('taskName')

                    if supervisor_data:
                        _send_notification(recipient_user_id=supervisor_data['id'], title="New Customer Request", body=f"A new request '{task_name}' has been assigned to you.", notification_type="NEW_CUSTOMER_REQUEST", data_payload={"taskId": str(new_id), "type": "NEW_CUSTOMER_REQUEST"}, fcm_token=supervisor_data.get('fcmToken'), delay_seconds=60)

                    if supervisor_data and supervisor_data.get('reportingToId'):
                        office_admin_user_id = supervisor_data.get('reportingToId')
                        admin_email_result = execute_query('SELECT "officeAdminEmail" FROM "AdminSettings" WHERE COALESCE("isDeleted", 0) = 0 LIMIT 1', fetch=True)
                        admin_email = admin_email_result[0]['officeAdminEmail'] if admin_email_result else None
                        admin_title = "FYI: New Customer Request Assigned"
                        admin_body = f"Request '{task_name}' has been assigned to Supervisor '{supervisor_data.get('fullName', supervisorId)}'."
                        _send_notification(recipient_user_id=office_admin_user_id, title=admin_title, body=admin_body, notification_type="NEW_REQUEST_ADMIN_LOG", data_payload={"taskId": str(new_id), "type": "NEW_REQUEST_ADMIN_LOG"}, fcm_token=None)
                        if admin_email and email_config:
                            admin_context = {
                            "Task Name": task_name,
                            "Supervisor Name": supervisor_data.get('fullName', supervisorId),
                            "Customer Name": customer_data.get('customerName', 'N/A') if customer_data else 'N/A'
                        }
                        # Call the new dynamic email function
                        send_mail_with_template("New Request Admin Notification", admin_email, admin_context)

                    if customer_data and customer_data.get('email') and email_config:
                        customer_context = {
                        "Customer Name": customer_data.get('customerName', 'Customer'),
                        "Task Name": task_name,
                        "Task id": new_id
                    }
                    # Call the new dynamic email function
                    send_mail_with_template("Customer Request Received", customer_data.get('email'), customer_context)

            elif job_type == 1 and supervisorId and getattr(creating_user, 'employeeId', None) != supervisorId: # Use getattr for safety
                    # Internal Issue assignment notification
                print(f"Job type is 1 and creator ({getattr(creating_user, 'employeeId', 'N/A')}) is not the supervisor ({supervisorId}). Attempting internal issue notification.")
                try:
                    fcm_query = 'SELECT "fcmToken", id FROM "user" WHERE "employeeId" = %s'
                    fcm_result = execute_query(fcm_query, [supervisorId], fetch=True)
                    amc_job_name = result_from_amc.get('amcJobName') if result_from_amc else None

                    if fcm_result:
                        supervisor_record = fcm_result[0] 
                        if supervisor_record.get('fcmToken'):
                            title = "New issue assigned"
                            body = f"A new issue '{task_name}' is assigned to you by {getattr(creating_user, 'fullName', creating_user.userName) or getattr(creating_user, 'userName', 'N/A')}." # Safely get name
                            if amc_job_name: body = f"A new issue for AMC '{amc_job_name}' is created by {getattr(creating_user, 'fullName', creating_user.userName) or getattr(creating_user, 'userName', 'N/A')}." # Safely get name
                            _send_notification(recipient_user_id=supervisor_record.get('id'), title=title, body=body, notification_type="NEW_ISSUE_ASSIGNMENT", data_payload={"taskId": str(new_id), "type": "NEW_ISSUE_ASSIGNMENT"}, fcm_token=supervisor_record.get('fcmToken'), delay_seconds=60)
                except Exception as e:
                    print(f"ERROR: Failed to send internal supervisor notification for new task {new_id}. Error: {str(e)}")
            
            # --- CRITICAL CHANGE HERE ---
            # A customer cannot be an office admin. Only a 'User' (employee) can be.
            # Use `getattr(creating_user, 'is_customer', False)` to check if it's a customer.
            # If it's a customer, `is_office_admin` should always be False.
            
            is_office_admin = False
            # Check if it's NOT a customer, and if it's a User, then check its roleId
            if not getattr(creating_user, 'is_customer', False):
                # Now, if it's a User model instance, access its roleId foreign key's roleId
                # If for some reason 'creating_user' is an employee SimpleNamespace
                # (which it shouldn't be if 'auth_type == user_access' correctly fetches User.objects.get)
                # we'll still use getattr defensively.
                
                # Assuming 'creating_user' is a User model instance here (as per your get_user for 'user_access')
                # roleId is a ForeignKey, so creating_user.roleId gives a Userrole object.
                # To get the primary key of the role, it's creating_user.roleId.roleId
                
                # Safely get the role's primary key
                user_role_pk = None
                if hasattr(creating_user, 'roleId'):
                    # Access the roleId from the Userrole object
                    if hasattr(creating_user.roleId, 'roleId'):
                        user_role_pk = creating_user.roleId.roleId
                    # Fallback for unexpected direct integer on FK (less likely with proper model)
                    elif isinstance(creating_user.roleId, int):
                         user_role_pk = creating_user.roleId

                is_office_admin = (user_role_pk == 2)
            
            if is_office_admin and supervisorId and not was_requested and job_type != 1:
                print(f"PATH 3: Office Admin creating a task. Notifying supervisor.")
                try:
                    fcm_query = 'SELECT "fcmToken", id FROM "user" WHERE "employeeId" = %s'
                    fcm_result = execute_query(fcm_query, [supervisorId], fetch=True)
                    if fcm_result and fcm_result[0].get('fcmToken'):
                        supervisor_record = fcm_result[0]
                        title = "New Task Assigned"
                        body = f"A new task '{data.get('taskName')}' has been assigned to you by {getattr(creating_user, 'fullName', creating_user.userName) or getattr(creating_user, 'userName', 'N/A')}." # Safely get name
                        _send_notification(
                            recipient_user_id=supervisor_record.get('id'), title=title, body=body,
                            notification_type="NEW_TASK_ASSIGNMENT", data_payload={"taskId": str(new_id), "type": "NEW_TASK_ASSIGNMENT"},
                            fcm_token=supervisor_record.get('fcmToken'), delay_seconds=20
                        )
                except Exception as e:
                    print(f"ERROR during Office Admin->Supervisor notification: {str(e)}")

            # --- Logging and Response ---
            log_category = 'Issue' if task_type == ISSUE else 'Task'
            customer_result1 = execute_query('SELECT email, "customerName" FROM customer WHERE "customerId" = %s', [customerId], fetch=True)
            customer_name1 = customer_result1[0].get('customerName') if customer_result1 else 'N/A'
            # print("Customer Name for logging:", customer_name1)

            if was_requested: log_category = 'Request'
            log_activity_raw(request=request, category=log_category, action='Add', performer=creating_user, details={'id': new_id, 'title': data.get('taskName'), 'customerName': customer_name1})
            
            # Fetch teamLeaderName if teamLeaderId was provided
            response_data = {"id": new_id}
            if teamLeaderId:
                team_leader_result = execute_query('SELECT "fullName", name FROM "user" WHERE "employeeId" = %s AND COALESCE("isDeleted", 0) = 0', [teamLeaderId], fetch=True)
                if team_leader_result:
                    team_leader_name = team_leader_result[0].get('fullName') or team_leader_result[0].get('name')
                    response_data["teamLeaderId"] = teamLeaderId
                    response_data["teamLeaderName"] = team_leader_name
            
            message = "Request created successfully." if was_requested else (ISSUE_CREATED_SUCCESSFULLY if task_type == 1 else TASK_CREATED_SUCCESSFULLY)
            return success_response(data=response_data, message=message, status_code=status.HTTP_201_CREATED)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error creating record: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def put(self, request, task_id):
        """
        Updates an existing Task, Issue, or Request.
        - For Requests, handles specific actions for Approval (status=1) and Rejection (status=2).
        - Sends a notification to the customer when their request is approved or rejected.
        - Sends a corresponding notification to the assigned supervisor.
        - Only updates columns that were actually provided.
        """
        try:
            data = getattr(request, 'data', None) or request.POST
            image_files = request.FILES.getlist('images') if hasattr(request, 'FILES') else []
            audio_files = request.FILES.getlist('audio') if hasattr(request, 'FILES') else []

            # Fetch the original task record, including fields needed for notifications
            task_query = """
                SELECT id, "requestId", "taskType", "wasRequested", "customerId", "taskName", "supervisorId", "taskStatus", "lastStatusDate"
                FROM "taskManager"
                WHERE id = %s AND COALESCE("isDeleted", 0) = 0
            """
            task_result = execute_query(task_query, [task_id], fetch='one')
            
            # Ensure the result is a dictionary
            if isinstance(task_result, list) and task_result:
                task_result = task_result[0]
            elif not task_result:
                 return error_response(message=RECORD_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

            if not task_result:
                return error_response(message=RECORD_NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)

            # --- [Update query building logic - This part is correct and unchanged] ---
            allowed_fields = [
                'taskName', 'customerId', 'villaId', 'jobType', 'priority', 'reminder',
                'supervisorId', 'teamLeaderId', 'startDate', 'dueDate', 'notes', 'approvalStatus',
                'taskType', 'taskStatus', 'amcJobId'
            ]
            integer_fields = [
                'villaId', 'amcJobId', 'jobType', 'priority', 'approvalStatus',
                'taskType', 'taskStatus'
            ]
            update_fields = []
            params = []
            for field in allowed_fields:
                if field in data:
                    raw_value = data.get(field)
                    if isinstance(raw_value, str) and raw_value.strip() == '': continue
                    if field in integer_fields:
                        if raw_value in (None, ''): continue
                        try: value = int(raw_value)
                        except (ValueError, TypeError):
                            return error_response(message=f"Invalid value for {field}", status_code=status.HTTP_400_BAD_REQUEST)
                    else: value = raw_value
                    update_fields.append(f'"{field}" = %s')
                    params.append(value)

            resolved_task_status = task_result.get("taskStatus")
            if 'taskStatus' in data:
                resolved_task_status = to_int_or_none(data.get('taskStatus'))

            if 'lastStatusDate' in data or 'taskStatus' in data:
                try:
                    # For any status update, always stamp current Dubai time unless
                    # an explicit lastStatusDate is provided by the client.
                    if 'taskStatus' in data and data.get('lastStatusDate') in (None, ''):
                        close_date = self._get_dubai_datetime()
                    else:
                        close_date = self._get_close_date_value(
                            resolved_task_status,
                            data.get('lastStatusDate'),
                            task_result.get('lastStatusDate')
                        )
                except ValueError as exc:
                    return error_response(message=str(exc), status_code=status.HTTP_400_BAD_REQUEST)
                update_fields.append('"lastStatusDate" = %s')
                params.append(close_date)

            if image_files:
                new_image_paths = save_task_manager_files(files=image_files, task_id=task_id, file_type='images', delete_existing=True)
                update_fields.append('"images" = %s')
                params.append(json.dumps(new_image_paths))
            if audio_files:
                new_audio_paths = save_task_manager_files(files=audio_files, task_id=task_id, file_type='audio', delete_existing=True)
                update_fields.append('"audio" = %s')
                params.append(json.dumps(new_audio_paths))
            if not update_fields:
                return success_response(message=NO_VALID_FIELDS_FOR_UPDATE)
            update_fields.append('"updatedAt" = CURRENT_TIMESTAMP')
            query = f'UPDATE "taskManager" SET {", ".join(update_fields)} WHERE id = %s;'
            params.append(task_id)
            execute_query(query, params)
            # --- [End of update query building] ---

            # print(data)
            # =========================================================================
            # --- LOGIC BLOCK FOR LOGGING, MESSAGING, AND NOTIFICATIONS ---
            # =========================================================================
            was_requested = task_result.get("wasRequested", False)
            task_type = task_result.get("taskType")

            # Start with default values
            log_category = 'Task'
            log_action = 'Update'
            message = TASK_UPDATED_SUCCESSFULLY

            previous_task_status = task_result.get("taskStatus")
            task_status_value = task_result.get("taskStatus")
            if 'taskStatus' in data:
                task_status_value = to_int_or_none(data.get('taskStatus'))

            # Determine the correct category, action, and message based on the task type
            if was_requested:
                log_category = 'Request'
                message = "Request updated successfully."

                if 'approvalStatus' in data:
                    try:
                        new_status = int(data.get('approvalStatus'))
                    except (ValueError, TypeError):
                        return error_response(message="Invalid approvalStatus value", status_code=status.HTTP_400_BAD_REQUEST)

                    customer_notification_title = None
                    customer_notification_body = None
                    customer_notification_type = None
                    task_name = data.get('taskName', task_result.get("taskName"))

                    if new_status == REQUEST_STATUS_APPROVED:
                        log_action = 'Approve'
                        message = "Request approved successfully."
                        customer_notification_title = "Request approved"
                        customer_notification_body = f"Your service request '{task_name}' is approved."
                        customer_notification_type = "REQUEST_APPROVED"

                    elif new_status == REQUEST_STATUS_REJECTED:
                        log_action = 'Reject'
                        message = "Request rejected successfully."
                        customer_notification_title = "Request rejected"
                        customer_notification_body = f"Your service request '{task_name}' is rejected."
                        customer_notification_type = "REQUEST_REJECTED"

                    # If a notification needs to be sent, do it here
                    if customer_notification_title:
                        customer_id = task_result.get("customerId")

                        # --- 1. NOTIFY THE CUSTOMER ---
                        if customer_id:
                            try:
                                fcm_query = 'SELECT "fcmToken" FROM "customer" WHERE "customerId" = %s'
                                fcm_result = execute_query(fcm_query, [customer_id], fetch='one')
                                if fcm_result and isinstance(fcm_result, list) and fcm_result[0].get('fcmToken'):
                                    customer_fcm_token = fcm_result[0]['fcmToken']
                                    data_payload = { "requestId": str(task_id), "type": customer_notification_type }
                                    logger.info(f"Queuing '{customer_notification_type}' notification for customer {customer_id}.")
                                    _send_notification(
                                        title=customer_notification_title, body=customer_notification_body,
                                        notification_type=customer_notification_type, data_payload=data_payload,
                                        fcm_token=customer_fcm_token, delay_seconds=60,
                                        recipient_customer_id=customer_id
                                    )
                                else:
                                    logger.warning(f"Customer {customer_id} not found or has no FCM token. Notification not sent.")
                            except Exception as e:
                                logger.error(f"Failed to send customer notification for task {task_id}. Error: {str(e)}")
                        else:
                            logger.warning(f"Cannot send notification for task {task_id}. Task has no associated customerId.")

                        # --- 2. NOTIFY THE ASSIGNED SUPERVISOR ---
                        assigned_supervisor_id = data.get('supervisorId', task_result.get("supervisorId"))

                        if assigned_supervisor_id:
                            try:
                                supervisor_title = ""
                                supervisor_body = ""
                                supervisor_noti_type = ""

                                if new_status == REQUEST_STATUS_APPROVED:
                                    if task_type == TASK:
                                            supervisor_title = "New task assigned"
                                            supervisor_body = f"The customer request '{task_name}' was approved and is now assigned as a task."
                                            supervisor_noti_type = "NEW_TASK_ASSIGNED"
                                    else:
                                        supervisor_title = "New issue assigned"
                                        supervisor_body = f"The customer request '{task_name}' was approved and is now assigned as a issue."
                                        supervisor_noti_type = "NEW_ISSUE_ASSIGNED"

                                elif new_status == REQUEST_STATUS_REJECTED:
                                    supervisor_title = "Customer request rejected"
                                    supervisor_body = f"The customer request '{task_name}', which was assigned to you, has been rejected."
                                    supervisor_noti_type = "ASSIGNED_REQUEST_REJECTED"

                                supervisor_query = 'SELECT id, "fcmToken" FROM "user" WHERE "employeeId" = %s'
                                supervisor_result = execute_query(supervisor_query, [assigned_supervisor_id], fetch='one')

                                if supervisor_result and isinstance(supervisor_result, list) and supervisor_result[0].get('fcmToken'):
                                    supervisor_data = supervisor_result[0]
                                    supervisor_payload = {"taskId": str(task_id), "type": supervisor_noti_type}

                                    logger.info(f"Queuing '{supervisor_noti_type}' notification for supervisor {assigned_supervisor_id}.")
                                    _send_notification(
                                        title=supervisor_title, body=supervisor_body,
                                        notification_type=supervisor_noti_type, data_payload=supervisor_payload,
                                        fcm_token=supervisor_data['fcmToken'],
                                        delay_seconds=60,
                                        recipient_user_id=supervisor_data['id']
                                    )
                                else:
                                    logger.warning(f"Assigned supervisor '{assigned_supervisor_id}' for task {task_id} not found or has no FCM token.")
                            except Exception as e:
                                logger.error(f"Failed to send supervisor notification for task {task_id}. Error: {str(e)}")
                        else:
                            logger.warning(f"No supervisor assigned to task {task_id}. Cannot send supervisor notification.")

                if 'taskStatus' in data and task_status_value != previous_task_status:
                    request_id_value = task_result.get("requestId") or task_id
                    customer_id = task_result.get("customerId")
                    current_supervisor_id = data.get('supervisorId', task_result.get("supervisorId"))

                    if task_status_value == QUOTATION_STAGE:
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_value}",
                                body=f"Emergency Request #{request_id_value} is now in quotation stage.",
                                notification_type="EMERGENCY_QUOTATION_STAGE_CUSTOMER",
                                data_payload={"taskId": str(task_id), "type": "EMERGENCY_QUOTATION_STAGE_CUSTOMER"},
                            )
                        estimators = self._get_estimator_users()
                        self._notify_users(
                            users=estimators,
                            title=f"Emergency Request #{request_id_value}",
                            body=f"Quotation required for Emergency Request #{request_id_value}.",
                            notification_type="EMERGENCY_QUOTATION_REQUIRED",
                            data_payload={"taskId": str(task_id), "type": "EMERGENCY_QUOTATION_REQUIRED"},
                        )

                    elif task_status_value == JOB_APPROVED:
                        supervisor_query = 'SELECT id, "fcmToken" FROM "user" WHERE "employeeId" = %s'
                        supervisor_result = execute_query(supervisor_query, [current_supervisor_id], fetch='one')
                        supervisor_data = supervisor_result[0] if isinstance(supervisor_result, list) and supervisor_result else (supervisor_result if isinstance(supervisor_result, dict) else None)
                        if supervisor_data and supervisor_data.get('fcmToken'):
                            self._notify_users(
                                users=[supervisor_data],
                                title=f"Emergency Request #{request_id_value}",
                                body=f"Emergency Request #{request_id_value} has been approved. Proceed with the work.",
                                notification_type="EMERGENCY_JOB_APPROVED_SUPERVISOR",
                                data_payload={"taskId": str(task_id), "type": "EMERGENCY_JOB_APPROVED_SUPERVISOR"},
                            )
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_value}",
                                body=f"Emergency Request #{request_id_value} is in progress and will be resolved as soon as possible.",
                                notification_type="EMERGENCY_JOB_APPROVED_CUSTOMER",
                                data_payload={"taskId": str(task_id), "type": "EMERGENCY_JOB_APPROVED_CUSTOMER"},
                            )

                    elif task_status_value == CANCELLED:
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_value}",
                                body=f"Emergency Request #{request_id_value} has been cancelled.",
                                notification_type="EMERGENCY_CANCELLED_CUSTOMER",
                                data_payload={"taskId": str(task_id), "type": "EMERGENCY_CANCELLED_CUSTOMER"},
                            )
                        office_admins = self._get_users_by_group_numbers([ROLE_GROUP_OFFICE_ADMIN])
                        self._notify_users(
                            users=office_admins,
                            title=f"Emergency Request #{request_id_value}",
                            body=f"Emergency Request #{request_id_value} has been cancelled.",
                            notification_type="EMERGENCY_CANCELLED_ADMIN",
                            data_payload={"taskId": str(task_id), "type": "EMERGENCY_CANCELLED_ADMIN"},
                        )

                    elif task_status_value == CLOSED:
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_value}",
                                body=f"Emergency Request #{request_id_value} has been completed successfully.",
                                notification_type="EMERGENCY_CLOSED_CUSTOMER",
                                data_payload={"taskId": str(task_id), "type": "EMERGENCY_CLOSED_CUSTOMER"},
                            )
                        office_admins = self._get_users_by_group_numbers([ROLE_GROUP_OFFICE_ADMIN])
                        self._notify_users(
                            users=office_admins,
                            title=f"Emergency Request #{request_id_value}",
                            body=f"Emergency Request #{request_id_value} has been completed and closed.",
                            notification_type="EMERGENCY_CLOSED_ADMIN",
                            data_payload={"taskId": str(task_id), "type": "EMERGENCY_CLOSED_ADMIN"},
                        )

            # --- Additional: For non-customer requests (regular Tasks/Issues) send similar
            # notifications to estimators / supervisors / admins as per reference list.
            # Note: Do NOT notify customers here; customer notifications are only
            # sent when wasRequested == True (handled above).
            if not was_requested and 'taskStatus' in data and task_status_value != previous_task_status:
                request_id_value = task_result.get("requestId") or task_id
                current_supervisor_id = data.get('supervisorId', task_result.get("supervisorId"))

                if task_status_value == QUOTATION_STAGE:
                    estimators = self._get_estimator_users()
                    self._notify_users(
                        users=estimators,
                        title=f"Task #{request_id_value}",
                        body=f"Quotation required for Task #{request_id_value}.",
                        notification_type="TASK_QUOTATION_REQUIRED",
                        data_payload={"taskId": str(task_id), "type": "TASK_QUOTATION_REQUIRED"},
                    )

                elif task_status_value == JOB_APPROVED:
                    if current_supervisor_id:
                        supervisor_query = 'SELECT id, "fcmToken" FROM "user" WHERE "employeeId" = %s'
                        supervisor_result = execute_query(supervisor_query, [current_supervisor_id], fetch='one')
                        supervisor_data = supervisor_result[0] if isinstance(supervisor_result, list) and supervisor_result else (supervisor_result if isinstance(supervisor_result, dict) else None)
                        if supervisor_data and supervisor_data.get('fcmToken'):
                            self._notify_users(
                                users=[supervisor_data],
                                title=f"Task #{request_id_value}",
                                body=f"Task #{request_id_value} has been approved. Proceed with the work.",
                                notification_type="TASK_JOB_APPROVED_SUPERVISOR",
                                data_payload={"taskId": str(task_id), "type": "TASK_JOB_APPROVED_SUPERVISOR"},
                            )

                elif task_status_value == CANCELLED:
                    office_admins = self._get_users_by_group_numbers([ROLE_GROUP_OFFICE_ADMIN])
                    self._notify_users(
                        users=office_admins,
                        title=f"Task #{request_id_value}",
                        body=f"Task #{request_id_value} has been cancelled.",
                        notification_type="TASK_CANCELLED_ADMIN",
                        data_payload={"taskId": str(task_id), "type": "TASK_CANCELLED_ADMIN"},
                    )

                elif task_status_value == CLOSED:
                    office_admins = self._get_users_by_group_numbers([ROLE_GROUP_OFFICE_ADMIN])
                    self._notify_users(
                        users=office_admins,
                        title=f"Task #{request_id_value}",
                        body=f"Task #{request_id_value} has been completed and closed.",
                        notification_type="TASK_CLOSED_ADMIN",
                        data_payload={"taskId": str(task_id), "type": "TASK_CLOSED_ADMIN"},
                    )

                elif task_status_value == IN_PROGRESS:
                    # Notify supervisor that work is in progress for this task/issue
                    if current_supervisor_id:
                        supervisor_query = 'SELECT id, "fcmToken" FROM "user" WHERE "employeeId" = %s'
                        supervisor_result = execute_query(supervisor_query, [current_supervisor_id], fetch='one')
                        supervisor_data = supervisor_result[0] if isinstance(supervisor_result, list) and supervisor_result else (supervisor_result if isinstance(supervisor_result, dict) else None)
                        if supervisor_data and supervisor_data.get('fcmToken'):
                            self._notify_users(
                                users=[supervisor_data],
                                title=f"Task #{request_id_value}",
                                body=f"Task #{request_id_value} is in progress.",
                                notification_type="TASK_IN_PROGRESS_SUPERVISOR",
                                data_payload={"taskId": str(task_id), "type": "TASK_IN_PROGRESS_SUPERVISOR"},
                            )

            elif task_type == ISSUE:
                log_category = 'Issue'
                message = ISSUE_UPDATED_SUCCESSFULLY

            # Now, with all variables correctly set, perform the logging and return the response
            log_activity_raw(
                request=request, category=log_category, action=log_action,
                performer=request.user, details={'id': task_id, 'title': data.get('taskName', task_result.get("taskName"))}
            )

            # Fetch and return teamLeaderName if teamLeaderId was updated
            response_data = {}
            if 'teamLeaderId' in data and data.get('teamLeaderId'):
                team_leader_id = data.get('teamLeaderId')
                team_leader_result = execute_query('SELECT "fullName", name FROM "user" WHERE "employeeId" = %s AND COALESCE("isDeleted", 0) = 0', [team_leader_id], fetch=True)
                if team_leader_result:
                    team_leader_name = team_leader_result[0].get('fullName') or team_leader_result[0].get('name')
                    response_data["teamLeaderId"] = team_leader_id
                    response_data["teamLeaderName"] = team_leader_name

            return success_response(data=response_data if response_data else None, message=message)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error updating record: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def delete(self, request):
        """
        Deletes multiple records (soft delete) with specific logging.
        """
        try:
            record_ids = request.data.get("taskIds", [])
            if not record_ids:
                return error_response(message=NO_RECORD_IDS_PROVIDED, status_code=status.HTTP_400_BAD_REQUEST)
            
            placeholders = ', '.join(['%s'] * len(record_ids))

            # Fetch details BEFORE deleting for accurate logging
            query_details = f'SELECT id, "taskName", "taskType", "wasRequested" FROM "taskManager" WHERE id IN ({placeholders})'
            tasks_to_delete = execute_query(query_details, record_ids, many=True)
            if not isinstance(tasks_to_delete, list): tasks_to_delete = []

            # Perform the delete operation
            query_delete = f'UPDATE "taskManager" SET "isDeleted" = 1, "updatedAt" = CURRENT_TIMESTAMP WHERE id IN ({placeholders})'
            execute_query(query_delete, record_ids)

            # Log each deletion with the correct category
            for task in tasks_to_delete:
                task_id = task['id']
                task_name = task.get('taskName', 'N/A')
                task_type = task.get('taskType')
                was_requested = task.get('wasRequested')

                # --- DYNAMIC LOGGING ---
                log_category = 'Task'
                if was_requested:
                    log_category = 'Request'
                elif task_type == ISSUE:
                    log_category = 'Issue'

                log_activity_raw(
                    request=request,
                    category=log_category,
                    action='Delete',
                    performer=request.user,
                    details={'id': task_id, 'title': task_name}
                )
                # --- END DYNAMIC LOGGING ---

            return success_response(message=f"{len(record_ids)} records deleted successfully")
        except Exception as e:
            return error_response(message=f"Error deleting records: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

import json
import traceback
import pytz
from datetime import datetime
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from ..utils import (
    error_response, success_response, execute_query, log_activity_raw,
    _send_notification,
)
from ..constants import *


# ---------------------------------------------------------------------------
# Status constants for materialRequest.requestStatus
# ---------------------------------------------------------------------------
MAT_STATUS_PENDING        = 0   # Submitted by teamleader, awaiting supervisor
MAT_STATUS_APPROVED       = 1   # Supervisor approved
MAT_STATUS_REJECTED       = 2   # Supervisor rejected
MAT_STATUS_DISPATCHED     = 3   # Stock manager dispatched items

VALID_MAT_STATUSES = [
    MAT_STATUS_PENDING,
    MAT_STATUS_APPROVED,
    MAT_STATUS_REJECTED,
    MAT_STATUS_DISPATCHED,
]

VALID_PRIORITIES = [0, 1, 2, 3]   # matches CHECK constraint

MAX_DB_INTEGER = 2147483647  # PostgreSQL integer max

ALLOWED_SORT_FIELDS = {
    "createdAt", "requestId", "amcId", "jobNumber",
    "priority", "requestStatus", "requestedBy",
}


class MaterialRequestView(APIView):
    permission_classes = [IsAuthenticated]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _generate_request_id(self):
        """
        Generates a sequential requestId like MR250601-0001.
        Format: MR + YYMMDD + '-' + 4-digit sequence (resets daily).
        Confirm final format with Gokul — swap prefix/format as needed.
        """
        dubai_tz = pytz.timezone("Asia/Dubai")
        today_prefix = datetime.now(dubai_tz).strftime("MR-%y%m-")

        seq_query = '''
            SELECT COALESCE(MAX(CAST(RIGHT("requestId", 4) AS INTEGER)), 0) AS last_seq
            FROM public."materialRequest"
            WHERE "requestId" LIKE %s
              AND COALESCE("isDeleted", 0) = 0
        '''
        result = execute_query(seq_query, [f"{today_prefix}%"], fetch="one")
        if isinstance(result, list) and result:
            result = result[0]
        last_seq = int((result or {}).get("last_seq") or 0)
        return f"{today_prefix}{last_seq + 1:04d}"

    def _get_user_details(self, user_id):
        if not user_id:
            return None
        row = execute_query(
            '''SELECT id, "userName", "fullName"
               FROM public."user"
               WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
               LIMIT 1''',
            [user_id], fetch="one",
        )
        if isinstance(row, list):
            row = row[0] if row else None
        return row if isinstance(row, dict) else None

    def _get_user_notification_target(self, user_id):
        """Return {id, fullName, fcmToken} for a user, or None."""
        if not user_id:
            return None
        row = execute_query(
            '''SELECT id, "fullName", "fcmToken"
               FROM public."user"
               WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
               LIMIT 1''',
            [user_id], fetch="one",
        )
        if isinstance(row, list):
            row = row[0] if row else None
        return row if isinstance(row, dict) else None

    def _get_users_by_group_numbers(self, group_numbers):
        """Return list of {id, fullName, fcmToken} for all active users in given role groups."""
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

    def _notify_user(self, user, title, body, notification_type, data_payload):
        """Send a notification to a single user dict {id, fcmToken, ...}."""
        if not user:
            return
        try:
            _send_notification(
                recipient_user_id=user.get('id'),
                title=title,
                body=body,
                notification_type=notification_type,
                data_payload=data_payload,
                fcm_token=user.get('fcmToken'),
                delay_seconds=0,
            )
        except Exception:
            pass

    def _notify_users(self, users, title, body, notification_type, data_payload):
        """Send a notification to a list of user dicts."""
        for user in users:
            self._notify_user(user, title, body, notification_type, data_payload)

    def _get_user_role_info(self, user_id):
        """Return (groupNumber, isTeamLeader) for a user by joining user → userrole."""
        if not user_id:
            return None, False
        result = execute_query(
            '''SELECT ur."groupNumber", ur."isTeamLeader"
               FROM public."user" u
               JOIN public."userrole" ur ON u."roleId" = ur."roleId"
               WHERE u.id = %s''',
            [user_id], fetch="one",
        )
        if isinstance(result, list):
            result = result[0] if result else None
        if isinstance(result, dict):
            return result.get("groupNumber"), bool(result.get("isTeamLeader"))
        return None, False

    def _get_user_reporting_to_id(self, user_id):
        if not user_id:
            return None
        row = execute_query(
            '''SELECT "reportingToId"
               FROM public."user"
               WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
               LIMIT 1''',
            [user_id], fetch="one",
        )
        if isinstance(row, list):
            row = row[0] if row else None
        return row.get("reportingToId") if isinstance(row, dict) else None

    def _get_amc_details(self, amc_id):
        row = execute_query(
            '''SELECT "amcId", "amcJobName"
               FROM public."AMCMaster"
               WHERE "amcId" = %s AND COALESCE("isDeleted", 0) = 0
               LIMIT 1''',
            [amc_id], fetch="one",
        )
        if isinstance(row, list):
            row = row[0] if row else None
        return row if isinstance(row, dict) else None

    def _get_items(self, material_request_db_id):
        rows = execute_query(
            '''SELECT id, "itemName", "requestedQuantity", "supervisorQuantity",
                      "dispatchedQuantity", "itemRemarks", "supervisorRemarks",
                      "stockRemarks", "teamLeaderRemarks"
               FROM public."materialRequestItems"
               WHERE "materialRequestId" = %s AND COALESCE("isDeleted", 0) = 0
               ORDER BY id ASC''',
            [material_request_db_id], fetch="all", many=True,
        )
        return rows if isinstance(rows, list) else []

    def _enrich_row(self, row):
        """Attach human-readable names and items list to a materialRequest row."""
        if not isinstance(row, dict):
            return row

        # requestedBy
        if row.get("requestedBy"):
            u = self._get_user_details(row["requestedBy"])
            if u:
                row["requestedByName"] = u.get("fullName") or u.get("userName")

        # supervisorId
        if row.get("supervisorId"):
            u = self._get_user_details(row["supervisorId"])
            if u:
                row["supervisorName"] = u.get("fullName") or u.get("userName")

        # teamLeaderId
        if row.get("teamLeaderId"):
            u = self._get_user_details(row["teamLeaderId"])
            if u:
                row["teamLeaderName"] = u.get("fullName") or u.get("userName")

        # stockManagerId
        if row.get("stockManagerId"):
            u = self._get_user_details(row["stockManagerId"])
            if u:
                row["stockManagerName"] = u.get("fullName") or u.get("userName")

        # items child rows
        row["items"] = self._get_items(row["id"])

        return row

    # ------------------------------------------------------------------
    # POST — Create a new material request
    # Maps: MaterialReqModel fields -> DB columns
    # ------------------------------------------------------------------
    @transaction.atomic
    def post(self, request):
        try:
            data = getattr(request, "data", None) or request.POST

            # ---- required fields ----
            amc_master_id  = data.get("amcId")
            job_number     = (data.get("jobNumber") or "").strip()
            items_raw      = data.get("items")   # list of MaterialItem dicts

            # ---- optional fields ----
            work_order_no      = (data.get("workOrderNo") or "").strip() or None
            priority           = data.get("priority", 1)
            requested_by       = data.get("requestedBy") or request.user.id
            supervisor_id      = data.get("supervisorId") or None
            team_leader_id     = data.get("teamLeaderId") or data.get("teamleaderId") or None
            supervisor_remarks = (data.get("supervisorRemarks") or "").strip() or None
            final_remarks      = (data.get("finalRemarks") or "").strip() or None
            request_status     = data.get("requestStatus") or data.get("status", MAT_STATUS_PENDING)
            item_remark        = (data.get("itemRemarks") or data.get("itemRemark") or "").strip() or None

            # ---- validate required ----
            if not amc_master_id:
                return error_response(
                    message="amcId is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not job_number:
                return error_response(
                    message="jobNumber is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not items_raw:
                return error_response(
                    message="items list is required and must not be empty.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # ---- parse / validate items ----
            if isinstance(items_raw, str):
                try:
                    items_raw = json.loads(items_raw)
                except (TypeError, ValueError):
                    return error_response(
                        message="items must be a valid JSON array.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
            if not isinstance(items_raw, list) or len(items_raw) == 0:
                return error_response(
                    message="items must be a non-empty list.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            for idx, item in enumerate(items_raw):
                if not isinstance(item, dict):
                    return error_response(
                        message=f"items[{idx}] must be an object.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if not (item.get("itemName") or "").strip():
                    return error_response(
                        message=f"items[{idx}].itemName is required.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    qty = int(item.get("requestedQuantity", 0))
                    if qty <= 0:
                        raise ValueError
                    if qty > MAX_DB_INTEGER:
                        return error_response(
                            message=f"requestedQuantity exceeds maximum allowed value ({MAX_DB_INTEGER}).",
                            status_code=status.HTTP_400_BAD_REQUEST,
                        )
                except (TypeError, ValueError):
                    return error_response(
                        message=f"requestedQuantity must be a positive integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

            # ---- cast / validate scalars ----
            try:
                amc_master_id = int(amc_master_id)
            except (TypeError, ValueError):
                return error_response(
                    message="amcId must be an integer.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                return error_response(
                    message="priority must be an integer.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if priority not in VALID_PRIORITIES:
                return error_response(
                    message=f"priority must be one of {VALID_PRIORITIES}.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            try:
                request_status = int(request_status)
            except (TypeError, ValueError):
                return error_response(
                    message="status must be an integer.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if request_status not in VALID_MAT_STATUSES:
                return error_response(
                    message=f"status must be one of {VALID_MAT_STATUSES}.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # ---- verify AMC exists and fetch amcJobName for denormalization ----
            amc = self._get_amc_details(amc_master_id)
            if not amc:
                return error_response(
                    message="AMC record not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            amc_job_name = amc.get("amcJobName") or ""

            # ---- auto-set requestStatus and IDs based on role ----
            # Supervisor (groupNumber 3) → auto-approve + set supervisorId
            # Team Leader (isTeamLeader=true) → keep pending + set teamLeaderId
            user_group, is_team_leader = self._get_user_role_info(request.user.id)
            if user_group == ROLE_GROUP_SUPERVISOR:
                request_status = MAT_STATUS_APPROVED
                if not supervisor_id:
                    supervisor_id = request.user.id
            elif is_team_leader:
                request_status = MAT_STATUS_PENDING
                if not team_leader_id:
                    team_leader_id = request.user.id
                if not supervisor_id:
                    supervisor_id = self._get_user_reporting_to_id(request.user.id)

            # ---- verify optional FK users ----
            if supervisor_id:
                try:
                    supervisor_id = int(supervisor_id)
                except (TypeError, ValueError):
                    return error_response(
                        message="supervisorId must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if not self._get_user_details(supervisor_id):
                    return error_response(
                        message="supervisorId user not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )

            if team_leader_id:
                try:
                    team_leader_id = int(team_leader_id)
                except (TypeError, ValueError):
                    return error_response(
                        message="teamleaderId must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if not self._get_user_details(team_leader_id):
                    return error_response(
                        message="teamleaderId user not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )

            # ---- generate requestId with retry for uniqueness ----
            inserted_header = None
            request_id = None

            insert_header_sql = '''
                INSERT INTO public."materialRequest"
                    ("requestId", "amcId", "amcJobName", "jobNumber", "workOrderNo",
                     priority, "requestStatus", "requestedBy", "supervisorId", "teamLeaderId",
                     "supervisorRemarks", "finalRemarks", "createdAt", "updatedAt", "isDeleted")
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s,
                     %s, %s, NOW(), NOW(), 0)
                RETURNING
                    id, "requestId", "amcId", "amcJobName", "jobNumber", "workOrderNo",
                    priority, "requestStatus", "requestedBy", "supervisorId", "teamLeaderId",
                    "supervisorRemarks", "finalRemarks", "createdAt", "updatedAt"
            '''

            for _ in range(3):
                request_id = self._generate_request_id()
                try:
                    inserted_header = execute_query(
                        insert_header_sql,
                        [
                            request_id, amc_master_id, amc_job_name, job_number, work_order_no,
                            priority, request_status, requested_by, supervisor_id, team_leader_id,
                            supervisor_remarks, final_remarks,
                        ],
                        fetch="one",
                    )
                    if isinstance(inserted_header, list):
                        inserted_header = inserted_header[0] if inserted_header else None
                    if inserted_header:
                        break
                except IntegrityError:
                    inserted_header = None
                    continue

            if not inserted_header:
                return error_response(
                    message="Failed to create material request.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            db_id = inserted_header["id"]

            # ---- insert items ----
            insert_item_sql = '''
                INSERT INTO public."materialRequestItems"
                    ("materialRequestId", "itemName", "requestedQuantity", "itemRemarks",
                     "createdAt", "updatedAt", "isDeleted")
                VALUES (%s, %s, %s, %s, NOW(), NOW(), 0)
            '''
            for item in items_raw:
                execute_query(
                    insert_item_sql,
                    [
                        db_id,
                        item["itemName"].strip(),
                        int(item["requestedQuantity"]),
                        (item.get("remarks") or item.get("itemRemarks") or item_remark or "").strip() or None,
                    ],
                    fetch=None,
                )

            # ---- enrich and return ----
            result = self._enrich_row(inserted_header)

            log_activity_raw(
                request=request,
                category="MaterialRequest",
                action="Add",
                performer=getattr(request, "user", None),
                details={
                    "requestId": request_id,
                    "amcId": amc_master_id,
                    "jobNumber": job_number,
                    "itemCount": len(items_raw),
                    "priority": priority,
                },
            )

            # ---- notifications on create ----
            try:
                if is_team_leader or user_group == ROLE_GROUP_TEAM_LEADER:
                    # Team leader submitted → notify their specific supervisor
                    notify_targets = []
                    if supervisor_id:
                        sup_target = self._get_user_notification_target(supervisor_id)
                        if sup_target:
                            notify_targets.append(sup_target)
                    else:
                        notify_targets = self._get_users_by_group_numbers([ROLE_GROUP_SUPERVISOR])

                    self._notify_users(
                        users=notify_targets,
                        title=f"New Material Request #{request_id}",
                        body=f"A material request #{request_id} for job {job_number} has been submitted and is pending your approval.",
                        notification_type="MATERIAL_REQUEST_SUBMITTED",
                        data_payload={"requestId": str(request_id), "type": "MATERIAL_REQUEST_SUBMITTED"},
                    )
                elif user_group == ROLE_GROUP_SUPERVISOR:
                    # Supervisor submitted (auto-approved) → notify desktop users
                    desktop_users = self._get_users_by_group_numbers([ROLE_GROUP_DESKTOP_USER])
                    self._notify_users(
                        users=desktop_users,
                        title=f"Material Request Approved #{request_id}",
                        body=f"Material request #{request_id} for job {job_number} has been approved. Please prepare the materials.",
                        notification_type="MATERIAL_REQUEST_APPROVED",
                        data_payload={"requestId": str(request_id), "type": "MATERIAL_REQUEST_APPROVED"},
                    )
            except Exception:
                pass

            return success_response(
                data=result,
                message="Material request created successfully.",
                status_code=status.HTTP_201_CREATED,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error creating material request: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # GET — Fetch one (by request_id) or list with filters + pagination
    # ------------------------------------------------------------------
    def get(self, request, request_id=None):
        try:
            # ---- single fetch ----
            if request_id:
                row = execute_query(
                    '''SELECT mr.id, mr."requestId", mr."amcId", mr."amcJobName",
                              mr."jobNumber", mr."workOrderNo", mr.priority, mr.notes,
                              mr."requestStatus", mr."requestedBy", mr."supervisorId",
                              mr."teamLeaderId", mr."supervisorRemarks", mr."finalRemarks",
                              mr."supervisorActionAt", mr."stockManagerId",
                              mr."stockActionAt", mr."createdAt", mr."updatedAt"
                       FROM public."materialRequest" mr
                       WHERE mr.id = %s AND COALESCE(mr."isDeleted", 0) = 0
                       LIMIT 1''',
                    [request_id], fetch="one",
                )
                if isinstance(row, list):
                    row = row[0] if row else None
                if not row:
                    return error_response(
                        message="Material request not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                return success_response(
                    data=self._enrich_row(row),
                    message="Material request fetched successfully.",
                )


            # ---- list fetch ----
            search             = request.query_params.get("search", "").strip()
            amc_id_filter      = request.query_params.get("amcId", "").strip()
            job_number_filter  = request.query_params.get("jobNumber", "").strip()
            status_filter      = request.query_params.get("status", "").strip()
            priority_filter    = request.query_params.get("priority", "").strip()
            supervisor_filter  = request.query_params.get("supervisorId", "").strip()
            team_leader_filter = (request.query_params.get("teamLeaderId") or request.query_params.get("teamleaderId", "")).strip()
            requested_by_filter= request.query_params.get("requestedBy", "").strip()
            employee_id_filter = request.query_params.get("employeeId", "").strip()
            start_date         = request.query_params.get("startDate", "").strip()
            end_date           = request.query_params.get("endDate", "").strip()

            sort_param         = request.query_params.get("sort", "createdAt").strip() or "createdAt"
            order_param        = request.query_params.get("order", "desc").strip().lower() or "desc"
            page               = int(request.query_params.get("page", 1))
            page_size          = int(request.query_params.get("pageSize", 20))
            is_export          = request.query_params.get("isExport", "false").lower() == "true"

            where_conditions = ['COALESCE(mr."isDeleted", 0) = 0']
            params = []

            if search:
                where_conditions.append('''
                    (
                        mr."requestId"   ILIKE %s OR
                        mr."jobNumber"   ILIKE %s OR
                        mr."amcJobName"  ILIKE %s OR
                        sup."fullName"   ILIKE %s OR
                        tl."fullName"    ILIKE %s
                    )
                ''')
                sp = f"%{search}%"
                params.extend([sp, sp, sp, sp, sp])

            if amc_id_filter:
                where_conditions.append('mr."amcId" = %s')
                params.append(amc_id_filter)

            if job_number_filter:
                where_conditions.append('mr."jobNumber" ILIKE %s')
                params.append(f"%{job_number_filter}%")

            if status_filter:
                where_conditions.append('mr."requestStatus" = %s')
                params.append(status_filter)

            if priority_filter:
                where_conditions.append('mr.priority = %s')
                params.append(priority_filter)

            if supervisor_filter:
                where_conditions.append('mr."supervisorId" = %s')
                params.append(supervisor_filter)

            if team_leader_filter:
                where_conditions.append('mr."teamLeaderId" = %s')
                params.append(team_leader_filter)

            if requested_by_filter:
                where_conditions.append('mr."requestedBy" = %s')
                params.append(requested_by_filter)

            # ---- employeeId role-based filter ----
            # Determines the employee's role and filters accordingly:
            #   - Supervisor  (groupNumber 3)  → shows requests where supervisorId = employeeId
            #   - Team Leader (isTeamLeader)   → shows requests where teamLeaderId = employeeId
            #   - Other roles                  → shows requests where requestedBy = employeeId
            if employee_id_filter:
                emp_group, emp_is_tl = self._get_user_role_info(employee_id_filter)
                if emp_group == ROLE_GROUP_SUPERVISOR:
                    where_conditions.append('mr."supervisorId" = %s')
                    params.append(employee_id_filter)
                elif emp_is_tl or emp_group == ROLE_GROUP_TEAM_LEADER:
                    where_conditions.append('mr."teamLeaderId" = %s')
                    params.append(employee_id_filter)
                else:
                    where_conditions.append('mr."requestedBy" = %s')
                    params.append(employee_id_filter)


            if start_date and end_date:
                where_conditions.append('mr."createdAt"::date BETWEEN %s AND %s')
                params.extend([start_date, end_date])
            elif start_date:
                where_conditions.append('mr."createdAt"::date >= %s')
                params.append(start_date)
            elif end_date:
                where_conditions.append('mr."createdAt"::date <= %s')
                params.append(end_date)

            sort_field = sort_param if sort_param in ALLOWED_SORT_FIELDS else "createdAt"
            order_dir  = "asc" if order_param == "asc" else "desc"

            where_clause = "WHERE " + " AND ".join(where_conditions)

            count_result = execute_query(
                f'''SELECT COUNT(*) AS total
                    FROM public."materialRequest" mr
                    LEFT JOIN public."user" sup ON mr."supervisorId" = sup.id
                    LEFT JOIN public."user" tl  ON mr."teamLeaderId" = tl.id
                    {where_clause}''',
                params, fetch="one",
            )
            if isinstance(count_result, list):
                count_result = count_result[0] if count_result else {}
            total_count = int((count_result or {}).get("total") or 0)

            list_sql = f'''
                SELECT mr.id, mr."requestId", mr."amcId", mr."amcJobName",
                       mr."jobNumber", mr."workOrderNo", mr.priority,
                       mr."requestStatus", mr."requestedBy", mr."supervisorId",
                       mr."teamLeaderId", mr."supervisorRemarks", mr."finalRemarks",
                       mr."supervisorActionAt", mr."stockManagerId",
                       mr."stockActionAt", mr."createdAt", mr."updatedAt",
                       rb."fullName"  AS "requestedByName",
                       sup."fullName" AS "supervisorName",
                       tl."fullName"  AS "teamLeaderName"
                FROM public."materialRequest" mr
                LEFT JOIN public."user" rb  ON mr."requestedBy" = rb.id
                LEFT JOIN public."user" sup ON mr."supervisorId" = sup.id
                LEFT JOIN public."user" tl  ON mr."teamLeaderId" = tl.id
                {where_clause}
                ORDER BY mr."{sort_field}" {order_dir}
            '''
            query_params = list(params)
            if not is_export and page_size > 0:
                list_sql += " LIMIT %s OFFSET %s"
                query_params.extend([page_size, (page - 1) * page_size])

            rows = execute_query(list_sql, query_params, many=True) or []

            # Attach items to each row
            for row in rows:
                row["items"] = self._get_items(row["id"])

            total_pages = (
                (total_count + page_size - 1) // page_size if page_size > 0 and not is_export else 1
            )

            return success_response(
                data={
                    "results": rows,
                    "pagination": {
                        "totalRecords": total_count,
                        "totalPages": total_pages,
                        "currentPage": page if not is_export else 1,
                        "pageSize": page_size if not is_export else total_count,
                    },
                },
                message="Material requests fetched successfully.",
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error fetching material requests: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # PUT — Update header fields and/or item quantities/remarks
    # ------------------------------------------------------------------
    @transaction.atomic
    def put(self, request, request_id):
        try:
            if not request_id:
                return error_response(
                    message="request_id is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            data = getattr(request, "data", None) or request.POST

            # Fetch existing record
            existing = execute_query(
                '''SELECT id, "requestId", "requestStatus", "supervisorId", "teamLeaderId",
                          "supervisorActionAt", "stockManagerId", "stockActionAt",
                          "amcId", "jobNumber", "amcJobName"
                   FROM public."materialRequest"
                   WHERE "id" = %s AND COALESCE("isDeleted", 0) = 0
                   LIMIT 1''',
                [request_id], fetch="one",
            )
            if isinstance(existing, list):
                existing = existing[0] if existing else None
            if not existing:
                return error_response(
                    message="Material request not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            db_id = existing["id"]

            # ---- collect updatable header fields ----
            update_fields  = []
            update_params  = []
            audit_actions  = []

            # amcId — update AMC reference
            amc_master_id = data.get("amcId")
            if amc_master_id not in (None, ""):
                try:
                    amc_master_id = int(amc_master_id)
                except (TypeError, ValueError):
                    return error_response(
                        message="amcId must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                amc = self._get_amc_details(amc_master_id)
                if not amc:
                    return error_response(
                        message="AMC record not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                update_fields.append('"amcId" = %s')
                update_params.append(amc_master_id)
                update_fields.append('"amcJobName" = %s')
                update_params.append(amc.get("amcJobName") or "")

            # jobNumber
            job_number = data.get("jobNumber")
            if job_number not in (None, ""):
                update_fields.append('"jobNumber" = %s')
                update_params.append(str(job_number).strip())

            # workOrderNo
            work_order_no = data.get("workOrderNo")
            if work_order_no not in (None, ""):
                update_fields.append('"workOrderNo" = %s')
                update_params.append(work_order_no.strip() or None)

            # priority
            priority = data.get("priority")
            if priority not in (None, ""):
                try:
                    priority = int(priority)
                except (TypeError, ValueError):
                    return error_response(
                        message="priority must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if priority not in VALID_PRIORITIES:
                    return error_response(
                        message=f"priority must be one of {VALID_PRIORITIES}.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                update_fields.append("priority = %s")
                update_params.append(priority)

            # notes
            notes = data.get("notes")
            if notes not in (None, ""):
                update_fields.append('notes = %s')
                update_params.append(notes.strip() or None)

            # status (requestStatus)
            new_status = data.get("requestStatus") or data.get("status")
            if new_status not in (None, ""):
                try:
                    new_status = int(new_status)
                except (TypeError, ValueError):
                    return error_response(
                        message="status must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if new_status not in VALID_MAT_STATUSES:
                    return error_response(
                        message=f"status must be one of {VALID_MAT_STATUSES}.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                update_fields.append('"requestStatus" = %s')
                update_params.append(new_status)
                audit_actions.append("StatusUpdate")

                # Auto-set timestamps based on status transitions
                if new_status in (MAT_STATUS_APPROVED, MAT_STATUS_REJECTED):
                    # Supervisor acted
                    if not existing.get("supervisorActionAt"):
                        update_fields.append('"supervisorActionAt" = NOW()')
                    # Auto-assign supervisorId if not already set and not explicitly provided
                    if not existing.get("supervisorId") and not data.get("supervisorId"):
                        update_fields.append('"supervisorId" = %s')
                        update_params.append(request.user.id)
                        audit_actions.append("SupervisorAssigned")
                elif new_status == MAT_STATUS_DISPATCHED:
                    # Stock manager dispatched
                    if not existing.get("stockActionAt"):
                        update_fields.append('"stockActionAt" = NOW()')

            # supervisorId + supervisorActionAt
            supervisor_id = data.get("supervisorId")
            if supervisor_id not in (None, ""):
                try:
                    supervisor_id = int(supervisor_id)
                except (TypeError, ValueError):
                    return error_response(
                        message="supervisorId must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if not self._get_user_details(supervisor_id):
                    return error_response(
                        message="supervisorId user not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                update_fields.append('"supervisorId" = %s')
                update_params.append(supervisor_id)
                if not existing.get("supervisorActionAt"):
                    update_fields.append('"supervisorActionAt" = NOW()')
                audit_actions.append("SupervisorAssigned")

            # supervisorRemarks
            supervisor_remarks = data.get("supervisorRemarks")
            if supervisor_remarks not in (None, ""):
                update_fields.append('"supervisorRemarks" = %s')
                update_params.append(supervisor_remarks.strip() or None)

            # teamLeaderId
            team_leader_id = data.get("teamLeaderId") or data.get("teamleaderId")
            if team_leader_id not in (None, ""):
                try:
                    team_leader_id = int(team_leader_id)
                except (TypeError, ValueError):
                    return error_response(
                        message="teamleaderId must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if not self._get_user_details(team_leader_id):
                    return error_response(
                        message="teamleaderId user not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                update_fields.append('"teamLeaderId" = %s')
                update_params.append(team_leader_id)
                audit_actions.append("TeamLeaderAssigned")

            # finalRemarks
            final_remarks = data.get("finalRemarks")
            if final_remarks not in (None, ""):
                update_fields.append('"finalRemarks" = %s')
                update_params.append(final_remarks.strip() or None)

            # stockManagerId + stockActionAt
            stock_manager_id = data.get("stockManagerId")
            if stock_manager_id not in (None, ""):
                try:
                    stock_manager_id = int(stock_manager_id)
                except (TypeError, ValueError):
                    return error_response(
                        message="stockManagerId must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                if not self._get_user_details(stock_manager_id):
                    return error_response(
                        message="stockManagerId user not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                update_fields.append('"stockManagerId" = %s')
                update_params.append(stock_manager_id)
                if not existing.get("stockActionAt"):
                    update_fields.append('"stockActionAt" = NOW()')

            # ---- update items (optional) ----
            items_raw = data.get("items")
            items_updated = False
            if items_raw is not None:
                if isinstance(items_raw, str):
                    try:
                        items_raw = json.loads(items_raw)
                    except (TypeError, ValueError):
                        return error_response(
                            message="items must be a valid JSON array.",
                            status_code=status.HTTP_400_BAD_REQUEST,
                        )

                if not isinstance(items_raw, list):
                    return error_response(
                        message="items must be a list.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

                # Fetch existing items to match by itemName when id is missing
                existing_items = self._get_items(db_id)
                existing_items_by_name = {}
                for ei in existing_items:
                    name_key = (ei.get("itemName") or "").strip().lower()
                    if name_key:
                        existing_items_by_name[name_key] = ei

                # Track which existing item IDs are referenced in this update
                referenced_item_ids = set()

                for idx, item in enumerate(items_raw):
                    item_id = item.get("id")  # existing item PK to update
                    item_name = (item.get("itemName") or "").strip()

                    # If no id provided, try to match by itemName
                    if not item_id and item_name:
                        matched = existing_items_by_name.get(item_name.lower())
                        if matched:
                            item_id = matched["id"]

                    if item_id:
                        # ---- Update existing item ----
                        referenced_item_ids.add(int(item_id))

                        # Check if item should be soft-deleted
                        if item.get("isDeleted") in (1, "1", True):
                            execute_query(
                                '''UPDATE public."materialRequestItems"
                                   SET "isDeleted" = 1, "updatedAt" = NOW()
                                   WHERE id = %s AND "materialRequestId" = %s
                                     AND COALESCE("isDeleted", 0) = 0''',
                                [item_id, db_id],
                                fetch=None,
                            )
                            items_updated = True
                            continue

                        item_fields  = []
                        item_params  = []

                        # itemName — allow renaming
                        if item_name:
                            item_fields.append('"itemName" = %s')
                            item_params.append(item_name)

                        if "requestedQuantity" in item:
                            rq = int(item["requestedQuantity"])
                            if rq > MAX_DB_INTEGER:
                                return error_response(
                                    message=f"requestedQuantity exceeds maximum allowed value ({MAX_DB_INTEGER}).",
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                )
                            item_fields.append('"requestedQuantity" = %s')
                            item_params.append(rq)
                        if "supervisorQuantity" in item:
                            sq = int(item["supervisorQuantity"]) if item["supervisorQuantity"] is not None else None
                            if sq is not None and sq > MAX_DB_INTEGER:
                                return error_response(
                                    message=f"supervisorQuantity exceeds maximum allowed value ({MAX_DB_INTEGER}).",
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                )
                            item_fields.append('"supervisorQuantity" = %s')
                            item_params.append(sq)
                        if "dispatchedQuantity" in item:
                            dq = int(item["dispatchedQuantity"]) if item["dispatchedQuantity"] is not None else None
                            if dq is not None and dq > MAX_DB_INTEGER:
                                return error_response(
                                    message=f"dispatchedQuantity exceeds maximum allowed value ({MAX_DB_INTEGER}).",
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                )
                            item_fields.append('"dispatchedQuantity" = %s')
                            item_params.append(dq)
                        if "remarks" in item or "itemRemarks" in item:
                            remarks_val = item.get("remarks") or item.get("itemRemarks") or ""
                            item_fields.append('"itemRemarks" = %s')
                            item_params.append(remarks_val.strip() or None)
                        if "supervisorRemarks" in item:
                            item_fields.append('"supervisorRemarks" = %s')
                            item_params.append((item["supervisorRemarks"] or "").strip() or None)
                        if "stockRemarks" in item:
                            item_fields.append('"stockRemarks" = %s')
                            item_params.append((item["stockRemarks"] or "").strip() or None)
                        if "teamLeaderRemarks" in item:
                            item_fields.append('"teamLeaderRemarks" = %s')
                            item_params.append((item["teamLeaderRemarks"] or "").strip() or None)

                        if item_fields:
                            item_fields.append('"updatedAt" = NOW()')
                            execute_query(
                                f'''UPDATE public."materialRequestItems"
                                    SET {", ".join(item_fields)}
                                    WHERE id = %s AND "materialRequestId" = %s
                                      AND COALESCE("isDeleted", 0) = 0''',
                                item_params + [item_id, db_id],
                                fetch=None,
                            )
                            items_updated = True
                    else:
                        # ---- Insert new item ----
                        if not item_name:
                            return error_response(
                                message=f"items[{idx}].itemName is required for new items.",
                                status_code=status.HTTP_400_BAD_REQUEST,
                            )
                        req_qty = item.get("requestedQuantity") or item.get("quantity")
                        try:
                            req_qty = int(req_qty) if req_qty else 1
                        except (TypeError, ValueError):
                            req_qty = 1

                        execute_query(
                            '''INSERT INTO public."materialRequestItems"
                                   ("materialRequestId", "itemName", "requestedQuantity",
                                    "itemRemarks", "createdAt", "updatedAt", "isDeleted")
                               VALUES (%s, %s, %s, %s, NOW(), NOW(), 0)''',
                            [
                                db_id,
                                item_name,
                                req_qty,
                                (item.get("remarks") or item.get("itemRemarks") or "").strip() or None,
                            ],
                            fetch=None,
                        )
                        items_updated = True

            # ---- persist header update ----
            if update_fields:
                update_fields.append('"updatedAt" = NOW()')
                updated = execute_query(
                    f'''UPDATE public."materialRequest"
                        SET {", ".join(update_fields)}
                        WHERE "id" = %s AND COALESCE("isDeleted", 0) = 0
                        RETURNING id, "requestId", "amcId", "amcJobName", "jobNumber",
                                  "workOrderNo", priority, notes, "requestStatus",
                                  "requestedBy", "supervisorId", "teamLeaderId",
                                  "supervisorRemarks", "finalRemarks",
                                  "supervisorActionAt", "stockManagerId",
                                  "stockActionAt", "createdAt", "updatedAt"''',
                    update_params + [db_id],
                    fetch="one",
                )
                if isinstance(updated, list):
                    updated = updated[0] if updated else None
                if not updated:
                    return error_response(
                        message="Failed to update material request.",
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
            else:
                # No header changes — re-fetch current state
                updated = execute_query(
                    '''SELECT id, "requestId", "amcId", "amcJobName", "jobNumber",
                              "workOrderNo", priority, notes, "requestStatus",
                              "requestedBy", "supervisorId", "teamLeaderId",
                              "supervisorRemarks", "finalRemarks",
                              "supervisorActionAt", "stockManagerId",
                              "stockActionAt", "createdAt", "updatedAt"
                       FROM public."materialRequest"
                       WHERE id = %s AND COALESCE("isDeleted", 0) = 0''',
                    [db_id], fetch="one",
                )
                if isinstance(updated, list):
                    updated = updated[0] if updated else {}

            result = self._enrich_row(updated)

            log_activity_raw(
                request=request,
                category="MaterialRequest",
                action=", ".join(audit_actions) or "Update",
                performer=getattr(request, "user", None),
                details={
                    "requestId": existing.get("requestId"),
                    "dbId": db_id,
                    "actions": audit_actions,
                    "itemsUpdated": items_updated,
                },
            )

            msg = "Material request updated successfully."
            if items_updated and not update_fields:
                msg = "Material request items updated successfully."

            # ---- notifications on status change ----
            try:
                mr_request_id = existing.get("requestId") or (updated.get("requestId") if isinstance(updated, dict) else "")
                mr_job_number = existing.get("jobNumber") or (updated.get("jobNumber") if isinstance(updated, dict) else "")

                if new_status == MAT_STATUS_APPROVED:
                    # Supervisor approved → notify desktop users (group 8)
                    desktop_users = self._get_users_by_group_numbers([ROLE_GROUP_DESKTOP_USER])
                    self._notify_users(
                        users=desktop_users,
                        title=f"Material Request Approved #{mr_request_id}",
                        body=f"Material request #{mr_request_id} for job {mr_job_number} has been approved. Please prepare the materials.",
                        notification_type="MATERIAL_REQUEST_APPROVED",
                        data_payload={"requestId": str(mr_request_id), "type": "MATERIAL_REQUEST_APPROVED"},
                    )

                elif new_status == MAT_STATUS_DISPATCHED:
                    # Materials dispatched → notify supervisor and team leader
                    notify_targets = []
                    sup_id = (updated.get("supervisorId") if isinstance(updated, dict) else None) or existing.get("supervisorId")
                    tl_id = (updated.get("teamLeaderId") if isinstance(updated, dict) else None) or existing.get("teamLeaderId")

                    if sup_id:
                        sup_target = self._get_user_notification_target(sup_id)
                        if sup_target:
                            notify_targets.append(sup_target)
                    if tl_id:
                        tl_target = self._get_user_notification_target(tl_id)
                        if tl_target:
                            notify_targets.append(tl_target)

                    self._notify_users(
                        users=notify_targets,
                        title=f"Materials Dispatched #{mr_request_id}",
                        body=f"Materials for request #{mr_request_id} (job {mr_job_number}) have been dispatched and are ready.",
                        notification_type="MATERIAL_REQUEST_DISPATCHED",
                        data_payload={"requestId": str(mr_request_id), "type": "MATERIAL_REQUEST_DISPATCHED"},
                    )
            except Exception:
                pass

            return success_response(
                data=result,
                message=msg,
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error updating material request: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # DELETE — Soft delete
    # ------------------------------------------------------------------
    @transaction.atomic
    def delete(self, request, request_id):
        try:
            if not request_id:
                return error_response(
                    message="request_id is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            deleted = execute_query(
                '''UPDATE public."materialRequest"
                   SET "isDeleted" = 1, "updatedAt" = NOW()
                   WHERE "id" = %s AND COALESCE("isDeleted", 0) = 0
                   RETURNING "id"''',
                [request_id], fetch="one",
            )
            if isinstance(deleted, list):
                deleted = deleted[0] if deleted else None
            if not deleted:
                return error_response(
                    message="Material request not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            log_activity_raw(
                request=request,
                category="MaterialRequest",
                action="Delete",
                performer=getattr(request, "user", None),
                details={"requestId": request_id},
            )

            return success_response(
                data=None,
                message="Material request deleted successfully.",
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error deleting material request: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

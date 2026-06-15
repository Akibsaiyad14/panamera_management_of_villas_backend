import traceback
import json
import os
from datetime import datetime, timedelta
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response, log_activity_raw
from api.views.customer_authentication import CombinedUserCustomerAuthentication, UserCustomerPermission
from api.views.authentication import AccessTokenInvalidationAuthentication


def _save_feedback_audio(files, storage_id, log_date):
    """
    Saves audio files to media/feedback/<storage_id>/<log_date>/ using
    the original filename from the upload (no timestamp prefix added).
    """
    saved_paths = []
    upload_dir = os.path.join(settings.MEDIA_ROOT, "feedback", str(storage_id), log_date)
    os.makedirs(upload_dir, exist_ok=True)

    for f in files:
        filename = f.name
        file_path = os.path.join(upload_dir, filename)

        # If a file with the same name already exists, suffix with a counter
        counter = 1
        base, ext = os.path.splitext(filename)
        while os.path.exists(file_path):
            filename = f"{base}_{counter}{ext}"
            file_path = os.path.join(upload_dir, filename)
            counter += 1

        with open(file_path, "wb+") as destination:
            for chunk in f.chunks():
                destination.write(chunk)

        relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT).replace("\\", "/")
        saved_paths.append(relative_path)

    return saved_paths


# ---------------------------------------------------------------------------
# Admin / Supervisor endpoint — standard employee JWT
# ---------------------------------------------------------------------------

class AmcFeedbackView(APIView):
    """
    Full CRUD for AMC feedback.
    Used by admin / supervisors.
    GET  /amcFeedback              — paginated list with filters & search
    GET  /amcFeedback/<id>         — single record
    POST /amcFeedback              — supervisor submits feedback
    PUT  /amcFeedback/<id>         — update own feedback
    DELETE /amcFeedback/<id>       — soft-delete
    """
    permission_classes = [IsAuthenticated]

    allowed_sort_fields = {
        "id", "amcId", "amcJobId", "rating", "submittedByType",
        "customerName", "supervisorName", "createdAt",
    }

    def _build_audio_urls(self, request, audio_json):
        if not audio_json:
            return []

        try:
            audio_paths = json.loads(audio_json) if isinstance(audio_json, str) else audio_json
        except (TypeError, ValueError):
            return []

        if not isinstance(audio_paths, list):
            return []

        media_url = settings.MEDIA_URL.rstrip("/")
        return [request.build_absolute_uri(f"{media_url}/{path}") for path in audio_paths]

    def _serialize_feedback_row(self, request, row):
        row_data = dict(row)
        row_data["audioUrls"] = self._build_audio_urls(request, row_data.get("audio"))
        optional_name_fields = [
            "gardenSupervisorName",
            "poolSupervisorName",
            "gardenTeamLeaderName",
            "poolTeamLeaderName",
        ]
        for field in optional_name_fields:
            if not row_data.get(field):
                row_data.pop(field, None)
        return row_data

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def get(self, request, feedback_id=None):
        try:
            if feedback_id:
                return self._get_single(request, feedback_id)
            return self._get_list(request)
        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error fetching AMC feedback: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _get_single(self, request, feedback_id):
        query = """
            SELECT
                f.id,
                f."amcId",
                m."amcJobName",
                m."villaId",
                v."villaName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gs."fullName" END AS "gardenSupervisorName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ps."fullName" END AS "poolSupervisorName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gtl."fullName" END AS "gardenTeamLeaderName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ptl."fullName" END AS "poolTeamLeaderName",
                f."amcJobId",
                f."customerId",
                c."customerName",
                f."supervisorId",
                u."fullName" AS "supervisorName",
                f."submittedByType",
                f."rating",
                f."feedbackText",
                f."audio",
                f."createdAt",
                f."updatedAt"
            FROM public."AmcFeedback" f
            LEFT JOIN public."AMCMaster" m ON f."amcId" = m."amcId"
            LEFT JOIN public."villaDetails" v ON m."villaId" = v."id"
            LEFT JOIN public."user" gs ON m."gardenSupervisorId" = gs."employeeId" AND COALESCE(gs."isDeleted", 0) = 0
            LEFT JOIN public."user" ps ON m."poolSupervisorId" = ps."employeeId" AND COALESCE(ps."isDeleted", 0) = 0
            LEFT JOIN public."user" gtl ON m."gardenTeamLeaderId" = gtl."employeeId" AND COALESCE(gtl."isDeleted", 0) = 0
            LEFT JOIN public."user" ptl ON m."poolTeamLeaderId" = ptl."employeeId" AND COALESCE(ptl."isDeleted", 0) = 0
            LEFT JOIN public.customer c      ON f."customerId" = c."customerId"
            LEFT JOIN public."user" u        ON f."supervisorId" = u.id
            LEFT JOIN public."AmcJobs" j       ON f."amcJobId" = j."amcJobId"
            LEFT JOIN (
                SELECT "amcJobId", MAX("visitType") AS "visitType", MAX("visitDate") AS "visitDate"
                FROM public."amcJobs" aj
                GROUP BY "amcJobId"
            ) aj ON f."amcJobId" = aj."amcJobId"
            WHERE f.id = %s AND f."isDeleted" = 0;
        """
        result = execute_query(query, [feedback_id], fetch="one")
        if not result:
            return error_response(
                message="Feedback not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        row = result[0] if isinstance(result, list) else result
        return success_response(data=self._serialize_feedback_row(request, row), message="Feedback retrieved successfully.")

    def _get_list(self, request):
        search = request.query_params.get("search", "").strip()
        start_date = request.query_params.get("startDate", "").strip()
        end_date = request.query_params.get("endDate", "").strip()
        amc_id = request.query_params.get("amcId", "").strip()
        amc_job_id = request.query_params.get("amcJobId", "").strip()
        customer_id = request.query_params.get("customerId", "").strip()
        supervisor_id = request.query_params.get("supervisorId", "").strip()
        submitted_by_type = request.query_params.get("submittedByType", "").strip()
        rating = request.query_params.get("rating", "").strip()
        team_leader_id = request.query_params.get("teamLeaderId", "").strip()
        sort_param = request.query_params.get("sort", "createdAt:desc").strip()

        try:
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
        except (ValueError, TypeError):
            return error_response(
                message="page and pageSize must be integers.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        conditions = ['f."isDeleted" = 0']
        params = []

        if team_leader_id:
            # Only supervisor feedbacks for AMCs where the team leader matches
            conditions.append('f."amcId" IN (SELECT "amcId" FROM public."AMCMaster" WHERE ("gardenTeamLeaderId" = %s OR "poolTeamLeaderId" = %s) AND "isDeleted" = 0)')
            params.extend([team_leader_id, team_leader_id])
            conditions.append('f."submittedByType" = \'supervisor\'')

        if search:
            conditions.append(
                '(c."customerName" ILIKE %s OR u."fullName" ILIKE %s OR f."feedbackText" ILIKE %s OR m."amcJobName" ILIKE %s)'
            )
            s = f"%{search}%"
            params.extend([s, s, s, s])

        if amc_id:
            conditions.append('f."amcId" = %s')
            params.append(int(amc_id))

        if amc_job_id:
            conditions.append('f."amcJobId" = %s')
            params.append(int(amc_job_id))

        if customer_id:
            conditions.append('f."customerId" = %s')
            params.append(customer_id)

        if supervisor_id:
            conditions.append('f."supervisorId" = %s')
            params.append(int(supervisor_id))

        # Only add generic submitted_by_type filter if not already handled by team leader logic
        if submitted_by_type in ('supervisor') and not team_leader_id:
            conditions.append('f."submittedByType" = %s')
            params.append(submitted_by_type)

        if rating:
            try:
                conditions.append('f."rating" = %s')
                params.append(int(rating))
            except ValueError:
                pass

        if start_date:
            conditions.append('DATE(f."createdAt") >= %s')
            params.append(start_date)

        if end_date:
            conditions.append('DATE(f."createdAt") <= %s')
            params.append(end_date)

        where_clause = "WHERE " + " AND ".join(conditions)

        # Sorting
        sort_expr = 'f."createdAt" DESC'
        if sort_param:
            parts = sort_param.split(":")
            sort_field = parts[0]
            sort_dir = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ("asc", "desc") else "DESC"
            if sort_field in self.allowed_sort_fields:
                col_map = {
                    "customerName": 'LOWER(TRIM(c."customerName"))',
                    "supervisorName": 'LOWER(TRIM(u."fullName"))',
                    "amcJobName": 'LOWER(TRIM(m."amcJobName"))',
                }
                col = col_map.get(sort_field, f'f."{sort_field}"')
                sort_expr = f"{col} {sort_dir}"

        base_query = """
            FROM public."AmcFeedback" f
            LEFT JOIN public."AMCMaster" m ON f."amcId" = m."amcId"
            LEFT JOIN public."user" gs ON m."gardenSupervisorId" = gs."employeeId" AND COALESCE(gs."isDeleted", 0) = 0
            LEFT JOIN public."user" ps ON m."poolSupervisorId" = ps."employeeId" AND COALESCE(ps."isDeleted", 0) = 0
            LEFT JOIN public."user" gtl ON m."gardenTeamLeaderId" = gtl."employeeId" AND COALESCE(gtl."isDeleted", 0) = 0
            LEFT JOIN public."user" ptl ON m."poolTeamLeaderId" = ptl."employeeId" AND COALESCE(ptl."isDeleted", 0) = 0
            LEFT JOIN public.customer c      ON f."customerId" = c."customerId"
            LEFT JOIN public."user" u        ON f."supervisorId" = u.id
            LEFT JOIN public."AmcJobs" j       ON f."amcJobId" = j."amcJobId"
            LEFT JOIN (
                SELECT "amcJobId", MAX("visitType") AS "visitType", MAX("visitDate") AS "visitDate"
                FROM public."AmcJobs" aj
                GROUP BY "amcJobId"
            ) aj ON f."amcJobId" = aj."amcJobId"
            LEFT JOIN public."villaDetails" v ON m."villaId" = v."id"
        """

        count_result = execute_query(
            f'SELECT COUNT(f.id) AS total {base_query} {where_clause}',
            params,
            fetch="one",
        )
        total_count = 0
        if count_result:
            row = count_result[0] if isinstance(count_result, list) else count_result
            total_count = row.get("total", 0) if isinstance(row, dict) else (row[0] if row else 0)

        total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0
        offset = (page - 1) * page_size

        records = execute_query(
            f"""
            SELECT
                f.id,
                f."amcId",
                m."amcJobName",
                m."villaId",
                v."villaName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gs."fullName" END AS "gardenSupervisorName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ps."fullName" END AS "poolSupervisorName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gtl."fullName" END AS "gardenTeamLeaderName",
                CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ptl."fullName" END AS "poolTeamLeaderName",
                f."amcJobId",
                f."customerId",
                c."customerName",
                COALESCE(j."visitDate", aj."visitDate") AS "visitDate",
                f."supervisorId",
                u."fullName" AS "supervisorName",
                f."submittedByType",
                f."rating",
                f."feedbackText",
                f."audio",
                f."createdAt",
                f."updatedAt"
            {base_query}
            {where_clause}
            ORDER BY {sort_expr}
            LIMIT %s OFFSET %s;
            """,
            params + [page_size, offset],
            many=True,
        )

        serialized_records = [self._serialize_feedback_row(request, row) for row in (records or [])]
        
        return success_response(
            data={
                "results": serialized_records,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                },
            },
            message="AMC feedbacks retrieved successfully.",
        )

    # ------------------------------------------------------------------
    # POST  — supervisor submits feedback
    # ------------------------------------------------------------------
    def post(self, request):
        try:
            data = request.data
            audio_files = request.FILES.getlist("audio")
            amc_id = data.get("amcId")
            feedback_text = data.get("feedbackText", "").strip()
            rating = data.get("rating")
            amc_job_id = data.get("amcJobId")

            if not amc_id:
                return error_response(
                    message="amcId is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not feedback_text:
                return error_response(
                    message="feedbackText is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if rating is not None and int(rating) not in range(1, 6):
                return error_response(
                    message="rating must be between 1 and 5.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # Verify AMC exists
            amc_check = execute_query(
                'SELECT "amcId", "amcJobName" FROM public."AMCMaster" WHERE "amcId" = %s AND "isDeleted" = 0',
                [amc_id],
                fetch="one",
            )
            if not amc_check:
                return error_response(
                    message="AMC not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            supervisor_id = request.user.id
            storage_id = amc_job_id or amc_id
            log_date = datetime.now().strftime("%Y-%m-%d")
            audio_paths = _save_feedback_audio(audio_files, storage_id, log_date) if audio_files else []

            result = execute_query(
                """
                INSERT INTO public."AmcFeedback"
                    ("amcId", "amcJobId", "supervisorId", "submittedByType",
                     "rating", "feedbackText", "audio", "isDeleted", "createdAt", "updatedAt")
                VALUES (%s, %s, %s, 'supervisor', %s, %s, %s::jsonb, 0, NOW(), NOW())
                RETURNING id, "amcId", "amcJobId", "supervisorId",
                          "submittedByType", "rating", "feedbackText", "audio", "createdAt";
                """,
                [amc_id, amc_job_id or None, supervisor_id, rating or None, feedback_text, json.dumps(audio_paths)],
                fetch="one",
            )

            inserted = result[0] if isinstance(result, list) else result

            log_activity_raw(
                request,
                category="AmcFeedback",
                action="Add",
                performer=request.user,
                details={
                    "amcId": amc_id,
                    "feedbackId": inserted.get("id") if isinstance(inserted, dict) else None,
                    "submittedByType": "supervisor",
                    "amcJobName": amc_check[0].get("amcJobName") if isinstance(amc_check, list) else amc_check.get("amcJobName"),
                },
            )

            update_supervisor_feedback = """
                UPDATE public."AmcJobs"
                SET "isSupervisorFeedback" = TRUE
                WHERE "amcJobId" = %s;
            """
            execute_query(update_supervisor_feedback, [amc_job_id])



            return success_response(
                data=self._serialize_feedback_row(request, inserted),
                message="Feedback submitted successfully.",
                status_code=status.HTTP_201_CREATED,
            )
        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error submitting feedback: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # PUT — update feedback (only supervisor who owns it, or admin)
    # ------------------------------------------------------------------
    def put(self, request, feedback_id):
        try:
            existing = execute_query(
                'SELECT * FROM public."AmcFeedback" WHERE id = %s AND "isDeleted" = 0',
                [feedback_id],
                fetch="one",
            )
            if not existing:
                return error_response(
                    message="Feedback not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            row = existing[0] if isinstance(existing, list) else existing
            amc_job_name = """
                SELECT "amcJobName" FROM public."AMCMaster" m
                LEFT JOIN public."AmcJobs" j ON m."amcId" = j."amcId"
                WHERE j."amcJobId" = %s
            """
            amc_job_name_result = execute_query(amc_job_name, [row.get("amcJobId")], fetch="one")
            amc_job_name = amc_job_name_result[0].get("amcJobName") if isinstance(amc_job_name_result, list) else amc_job_name_result.get("amcJobName")


            data = request.data
            feedback_text = data.get("feedbackText", row.get("feedbackText"))
            rating = data.get("rating", row.get("rating"))
            existing_audio = []
            if row.get("audio"):
                try:
                    existing_audio = json.loads(row.get("audio"))
                except (TypeError, ValueError):
                    existing_audio = []

            audio_files = request.FILES.getlist("audio")
            new_audio_paths = []
            if audio_files:
                storage_id = row.get("amcJobId") or row.get("amcId")
                log_date = datetime.now().strftime("%Y-%m-%d")
                new_audio_paths = _save_feedback_audio(audio_files, storage_id, log_date)

            updated_audio = existing_audio + new_audio_paths

            if rating is not None and int(rating) not in range(1, 6):
                return error_response(
                    message="rating must be between 1 and 5.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            execute_query(
                """
                UPDATE public."AmcFeedback"
                SET "feedbackText" = %s,
                    "rating"       = %s,
                    "audio"        = %s::jsonb,
                    "updatedAt"    = NOW()
                WHERE id = %s AND "isDeleted" = 0;
                """,
                [feedback_text, rating or None, json.dumps(updated_audio), feedback_id],
                fetch=None,
            )

            log_activity_raw(
                request,
                category="AmcFeedback",
                action="Update",
                performer=request.user,
                details={
                    "feedbackId": feedback_id,
                    "amcId": row.get("amcId"),
                    "amcJobName": amc_job_name,
                },
            )

            return success_response(message="Feedback updated successfully.")
        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error updating feedback: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # DELETE — soft delete
    # ------------------------------------------------------------------
    def delete(self, request, feedback_id):
        try:
            existing = execute_query(
                'SELECT id FROM public."AmcFeedback" WHERE id = %s AND "isDeleted" = 0',
                [feedback_id],
                fetch="one",
            )
            if not existing:
                return error_response(
                    message="Feedback not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            
            amc_job_name = """
                SELECT "amcJobName" FROM public."AMCMaster" m
                LEFT JOIN public."AmcJobs" j ON m."amcId" = j."amcId"
                LEFT JOIN public."AmcFeedback" f ON f."amcJobId" = j."amcJobId"
                WHERE f.id = %s
            """
            amc_job_name_result = execute_query(amc_job_name, [feedback_id], fetch="one")
            amc_job_name = amc_job_name_result[0].get("amcJobName") if isinstance(amc_job_name_result, list) else amc_job_name_result.get("amcJobName")


            execute_query(
                'UPDATE public."AmcFeedback" SET "isDeleted" = 1, "updatedAt" = NOW() WHERE id = %s',
                [feedback_id],
                fetch=None,
            )

            log_activity_raw(
                request,
                category="AmcFeedback",
                action="Delete",
                performer=request.user,
                details={"feedbackId": feedback_id, "amcJobName": amc_job_name},
            )

            return success_response(message="Feedback deleted successfully.")
        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error deleting feedback: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------------
# Customer endpoint — combined (employee JWT  OR  customer JWT)
# ---------------------------------------------------------------------------

class CustomerAmcFeedbackView(APIView):
    """
    Customer-facing feedback endpoint.
    Both customers (via customer JWT) and supervisors (via employee JWT) can use this.

    POST /customerAmcFeedback       — submit feedback
    GET  /customerAmcFeedback       — list own feedbacks (customer sees their own,
                                      supervisor sees feedbacks tied to their AMCs)
    GET  /customerAmcFeedback/<id>  — single record (ownership-checked)
    """
    authentication_classes = [AccessTokenInvalidationAuthentication]
    permission_classes = [IsAuthenticated]

    def _build_audio_urls(self, request, audio_json):
        if not audio_json:
            return []

        try:
            audio_paths = json.loads(audio_json) if isinstance(audio_json, str) else audio_json
        except (TypeError, ValueError):
            return []

        if not isinstance(audio_paths, list):
            return []

        media_url = settings.MEDIA_URL.rstrip("/")
        return [request.build_absolute_uri(f"{media_url}/{path}") for path in audio_paths]

    def _serialize_feedback_row(self, request, row):
        row_data = dict(row)
        row_data["audioUrls"] = self._build_audio_urls(request, row_data.get("audio"))
        optional_name_fields = ["supervisorName", "teamLeaderName"]
        for field in optional_name_fields:
            if not row_data.get(field):
                row_data.pop(field, None)
        return row_data

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def _get_user_role_order(self, user_id):
        """Returns the roleOrderId for an employee user, or None if not found."""
        result = execute_query(
            'SELECT ur."roleOrderId" FROM public."user" u '
            'JOIN public."userrole" ur ON u."roleId" = ur."roleId" '
            'WHERE u.id = %s AND COALESCE(u."isDeleted", \'0\') = \'0\'',
            [user_id],
            fetch="one",
        )
        if result:
            row = result[0] if isinstance(result, list) else result
            return row.get("roleOrderId")
        return None

    def get(self, request, feedback_id=None):
        try:
            is_customer = getattr(request.user, "is_customer", False)
            is_admin = False
            if not is_customer:
                role_order = self._get_user_role_order(request.user.id)
                is_admin = role_order is not None and role_order <= 2

            if feedback_id:
                return self._get_single(request, feedback_id, is_customer, is_admin)
            return self._get_list(request, is_customer, is_admin)
        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error fetching feedback: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _get_single(self, request, feedback_id, is_customer, is_admin=False):
        query = """
            SELECT
                f.id,
                f."amcId",
                m."amcJobName",
                m."villaId",
                v."villaName",
                COALESCE(
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gs."fullName" END,
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ps."fullName" END
                ) AS "supervisorName",
                COALESCE(
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gtl."fullName" END,
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ptl."fullName" END
                ) AS "teamLeaderName",
                f."amcJobId",
                f."customerId",
                c."customerName",
                COALESCE(j."visitDate", aj."visitDate") AS "visitDate",
                f."supervisorId",
                f."submittedByType",
                f."rating",
                f."feedbackText",
                f."createdAt",
                f."updatedAt"
            FROM public."AmcFeedback" f
            LEFT JOIN public."AMCMaster" m ON f."amcId" = m."amcId"
            LEFT JOIN public."villaDetails" v ON m."villaId" = v."id"
            LEFT JOIN public."user" gs ON m."gardenSupervisorId" = gs."employeeId" AND COALESCE(gs."isDeleted", 0) = 0
            LEFT JOIN public."user" ps ON m."poolSupervisorId" = ps."employeeId" AND COALESCE(ps."isDeleted", 0) = 0
            LEFT JOIN public."user" gtl ON m."gardenTeamLeaderId" = gtl."employeeId" AND COALESCE(gtl."isDeleted", 0) = 0
            LEFT JOIN public."user" ptl ON m."poolTeamLeaderId" = ptl."employeeId" AND COALESCE(ptl."isDeleted", 0) = 0
            LEFT JOIN public.customer c      ON f."customerId" = c."customerId"
            LEFT JOIN public."AmcJobs" j       ON f."amcJobId" = j."amcJobId"
            LEFT JOIN (
                SELECT "amcJobId", MAX("visitType") AS "visitType", MAX("visitDate") AS "visitDate"
                FROM public."AmcJobs"
                GROUP BY "amcJobId"
            ) aj ON f."amcJobId" = aj."amcJobId"
            WHERE f.id = %s AND f."isDeleted" = 0
        """
        params = [feedback_id]

        # Customers can only see their own feedback; admins see all
        if is_customer:
            custumer_id_result = execute_query(
                'SELECT "customerId" FROM public.customer WHERE id = %s',
                [request.user.id],
                fetch="one",
            )
            customer_id = custumer_id_result[0]["customerId"] if custumer_id_result else None
            query += ' AND f."customerId" = %s'
            params.append(customer_id)

        result = execute_query(query, params, fetch="one")
        if not result:
            return error_response(
                message="Feedback not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        row = result[0] if isinstance(result, list) else result
        print(f"Fetched feedback for ID {feedback_id}: {row}")
        return success_response(data=self._serialize_feedback_row(request, row), message="Feedback retrieved successfully.")

    def _get_list(self, request, is_customer, is_admin=False):
        try:
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
        except (ValueError, TypeError):
            return error_response(
                message="page and pageSize must be integers.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        amc_id = request.query_params.get("amcId", "").strip()
        amc_job_id = request.query_params.get("amcJobId", "").strip()
        start_date = request.query_params.get("startDate", "").strip()
        end_date = request.query_params.get("endDate", "").strip()
        rating = request.query_params.get("rating", "").strip()
        sort_param = request.query_params.get("sort", "createdAt:desc").strip()
        customer_id = request.query_params.get("customerId", "").strip()
        supervisor_id = request.query_params.get("supervisorId", "").strip()
        team_leader_id = request.query_params.get("teamLeaderId", "").strip()

        conditions = ['f."isDeleted" = 0']
        params = []

        if is_customer:
            # Customers see only their own feedback
            cust_id = execute_query(
                'SELECT "customerId" FROM public.customer WHERE id = %s',
                [request.user.id],
                fetch="one",
            )
            _customer_id = cust_id[0]["customerId"] if cust_id else None
            conditions.append('f."customerId" = %s')
            params.append(_customer_id)
        elif is_admin:
            # Admins see all customer-submitted feedbacks across all AMCs
            conditions.append('f."submittedByType" = %s')
            params.append('customer')
        else:
            # Supervisors see only customer feedback for AMCs they supervise
            emp_result = execute_query(
                'SELECT "employeeId" FROM public."user" WHERE id = %s',
                [request.user.id],
                fetch="one",
            )
            emp_id = (emp_result[0] if isinstance(emp_result, list) else emp_result).get("employeeId") if emp_result else None
            conditions.append('f."amcId" IN (SELECT "amcId" FROM public."AMCMaster" WHERE ("gardenSupervisorId" = %s OR "poolSupervisorId" = %s) AND "isDeleted" = 0)')
            params.extend([emp_id, emp_id])
            conditions.append('f."submittedByType" = %s')
            params.append('customer')

        if amc_id:
            conditions.append('f."amcId" = %s')
            params.append(int(amc_id))

        if amc_job_id:
            conditions.append('f."amcJobId" = %s')
            params.append(int(amc_job_id))

        if rating:
            try:
                conditions.append('f."rating" = %s')
                params.append(int(rating))
            except ValueError:
                pass

        if start_date:
            conditions.append('DATE(f."createdAt") >= %s')
            params.append(start_date)

        if end_date:
            conditions.append('DATE(f."createdAt") <= %s')
            params.append(end_date)

        # Extra filters — only applied for non-customer callers to avoid conflicts
        if not is_customer and customer_id:
            conditions.append('f."customerId" = %s')
            params.append(customer_id)

        if supervisor_id:
            conditions.append('f."amcId" IN (SELECT "amcId" FROM public."AMCMaster" WHERE ("gardenSupervisorId" = %s OR "poolSupervisorId" = %s) AND "isDeleted" = 0)')
            params.extend([supervisor_id, supervisor_id])

        if team_leader_id:
            conditions.append('f."amcId" IN (SELECT "amcId" FROM public."AMCMaster" WHERE ("gardenTeamLeaderId" = %s OR "poolTeamLeaderId" = %s) AND "isDeleted" = 0)')
            params.extend([team_leader_id, team_leader_id])
            
        

        where_clause = "WHERE " + " AND ".join(conditions)

        sort_expr = 'f."createdAt" DESC'
        if sort_param:
            parts = sort_param.split(":")
            sort_field = parts[0]
            sort_dir = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ("asc", "desc") else "DESC"
            allowed = {"id", "amcId", "amcJobId", "rating", "createdAt"}
            if sort_field in allowed:
                sort_expr = f'f."{sort_field}" {sort_dir}'

        base_query = """
            FROM public."AmcFeedback" f
            LEFT JOIN public."AMCMaster" m ON f."amcId" = m."amcId"
            LEFT JOIN public."user" gs ON m."gardenSupervisorId" = gs."employeeId" AND COALESCE(gs."isDeleted", 0) = 0
            LEFT JOIN public."user" ps ON m."poolSupervisorId" = ps."employeeId" AND COALESCE(ps."isDeleted", 0) = 0
            LEFT JOIN public."user" gtl ON m."gardenTeamLeaderId" = gtl."employeeId" AND COALESCE(gtl."isDeleted", 0) = 0
            LEFT JOIN public."user" ptl ON m."poolTeamLeaderId" = ptl."employeeId" AND COALESCE(ptl."isDeleted", 0) = 0
            LEFT JOIN public.customer c      ON f."customerId" = c."customerId"
            LEFT JOIN public."AmcJobs" j       ON f."amcJobId" = j."amcJobId"
            LEFT JOIN (
                SELECT "amcJobId", MAX("visitType") AS "visitType", MAX("visitDate") AS "visitDate"
                FROM public."AmcJobs"
                GROUP BY "amcJobId"
            ) aj ON f."amcJobId" = aj."amcJobId"
            LEFT JOIN public."villaDetails" v ON m."villaId" = v."id"
        """

        count_result = execute_query(
            f'SELECT COUNT(f.id) AS total {base_query} {where_clause}',
            params,
            fetch="one",
        )
        total_count = 0
        if count_result:
            row = count_result[0] if isinstance(count_result, list) else count_result
            total_count = row.get("total", 0) if isinstance(row, dict) else (row[0] if row else 0)

        total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0
        offset = (page - 1) * page_size

        records = execute_query(
            f"""
            SELECT
                f.id,
                f."amcId",
                m."amcJobName",
                m."villaId",
                v."villaName",
                COALESCE(
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gs."fullName" END,
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ps."fullName" END
                ) AS "supervisorName",
                COALESCE(
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 1 THEN gtl."fullName" END,
                    CASE WHEN COALESCE(j."visitType", aj."visitType") = 2 THEN ptl."fullName" END
                ) AS "teamLeaderName",
                f."amcJobId",
                f."customerId",
                c."customerName",
                f."supervisorId",
                f."submittedByType",
                f."rating",
                f."feedbackText",
                f."createdAt",
                f."updatedAt",
                COALESCE(j."visitDate", aj."visitDate") AS "visitDate"
            {base_query}
            {where_clause}
            ORDER BY {sort_expr}
            LIMIT %s OFFSET %s;
            """,
            params + [page_size, offset],
            many=True,
        )

        serialized_records = [self._serialize_feedback_row(request, row) for row in (records or [])]

        return success_response(
            data={
                "results": serialized_records,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                },
            },
            message="Feedbacks retrieved successfully.",
        )

    # ------------------------------------------------------------------
    # POST — only customers submit feedback
    # ------------------------------------------------------------------
    def post(self, request):
        try:
            is_customer = getattr(request.user, "is_customer", False)
            data = request.data
            audio_files = request.FILES.getlist("audio")

            amc_id = data.get("amcId")
            feedback_text = data.get("feedbackText", "").strip()
            
            rating = data.get("rating")
            amc_job_id = data.get("amcJobId")
            date = data.get("date")

            if not amc_id:
                return error_response(
                    message="amcId is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not feedback_text:
                return error_response(
                    message="feedbackText is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if rating is not None and int(rating) not in range(1, 6):
                return error_response(
                    message="rating must be between 1 and 5.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # Verify AMC exists
            amc_check = execute_query(
                'SELECT "amcId", "amcJobName" FROM public."AMCMaster" WHERE "amcId" = %s AND "isDeleted" = 0',
                [amc_id],
                fetch="one",
            )
            amc_job_name = amc_check[0].get("amcJobName") if isinstance(amc_check, list) else amc_check.get("amcJobName") if amc_check else None
            if not amc_check:
                return error_response(
                    message="AMC not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            print(f"User ID: {request.user.id}, is_customer: {is_customer}")
            cus_id = request.user.id if is_customer else None
            submitted_by_type = "customer" if is_customer else "supervisor"

            # Resolve the supervisor from the AMC job's visitType
            supervisor_id = None
            if amc_job_id:
                sup_result = execute_query(
                    """
                    SELECT
                        CASE
                            WHEN j."visitType" = 1 THEN gs.id
                            WHEN j."visitType" = 2 THEN ps.id
                        END AS "supervisorUserId"
                    FROM public."AmcJobs" j
                    JOIN public."AMCMaster" m ON j."amcId" = m."amcId"
                    LEFT JOIN public."user" gs ON m."gardenSupervisorId" = gs."employeeId"
                        AND COALESCE(gs."isDeleted", 0) = 0
                    LEFT JOIN public."user" ps ON m."poolSupervisorId" = ps."employeeId"
                        AND COALESCE(ps."isDeleted", 0) = 0
                    WHERE j."amcJobId" = %s
                    """,
                    [amc_job_id],
                    fetch="one",
                )
                if sup_result:
                    row = sup_result[0] if isinstance(sup_result, list) else sup_result
                    supervisor_id = row.get("supervisorUserId")
            storage_id = amc_job_id or amc_id
            log_date = datetime.now().strftime("%Y-%m-%d")
            audio_paths = _save_feedback_audio(audio_files, storage_id, log_date) if audio_files else []


            customerid = execute_query(
            'SELECT "customerId", "customerName" FROM public.customer WHERE id = %s',
                [cus_id],
                fetch="one",
            )
            
            customer_id = customerid[0]['customerId'] if customerid else None
            customer_name = customerid[0]['customerName'] if customerid else None
            # print(f"Customer ID for user {request.user.id}: {customer_id}")

            # For customers: validate the AMC belongs to them
            if is_customer:
                ownership_check = execute_query(
                    'SELECT "amcId" FROM public."AMCMaster" WHERE "amcId" = %s AND "customerId" = %s AND "isDeleted" = 0',
                    [amc_id, customer_id],
                    fetch="one",
                )
                if not ownership_check:
                    return error_response(
                        message="You are not authorised to submit feedback for this AMC.",
                        status_code=status.HTTP_403_FORBIDDEN,
                    )

            result = execute_query(
                """
                INSERT INTO public."AmcFeedback"
                    ("amcId", "amcJobId", "customerId", "supervisorId", "submittedByType",
                     "rating", "feedbackText", "audio", "isDeleted", "createdAt", "updatedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 0, NOW(), NOW())
                RETURNING id, "amcId", "amcJobId", "customerId", "supervisorId",
                          "submittedByType", "rating", "feedbackText", "audio", "createdAt";
                """,
                [
                    amc_id,
                    amc_job_id or None,
                    customer_id,
                    supervisor_id,
                    submitted_by_type,
                    rating or None,
                    feedback_text,
                    json.dumps(audio_paths),
                ],
                fetch="one",
            )

            inserted = result[0] if isinstance(result, list) else result

            update_customer_feedback = """
                UPDATE public."AmcJobs"
                SET "isCustomerFeedback" = TRUE
                WHERE "amcId" = %s AND DATE("visitDate") = %s;
            """
            execute_query(update_customer_feedback, [amc_id, date])

            log_activity_raw(
                request,
                category="AmcFeedback",
                action="CustomerAdd",
                performer=request.user,
                details={
                    "amcId": amc_id,
                    "feedbackId": inserted.get("id") if isinstance(inserted, dict) else None,
                    "customerId": customer_id,
                    "customerName": customer_name,
                    "submittedByType": submitted_by_type,
                    "amcJobName": amc_job_name,
                },
            )

            return success_response(
                data=self._serialize_feedback_row(request, inserted),
                message="Feedback submitted successfully.",
                status_code=status.HTTP_201_CREATED,
            )
        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error submitting feedback: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

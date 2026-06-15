from rest_framework.views import APIView
from django.http import HttpResponseBadRequest
from dateutil.parser import parse
import json
import traceback
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.conf import settings
from api.utils import success_response, error_response, execute_query, save_files_task_comments_issues, to_int_or_none, format_date, detect_attachment_type
# Assume execute_query, success_response, error_response, to_int_or_none, save_files_task_comments_issues are defined as in previous examples
# save_files_task_comments_issues: adapted from save_task_manager_files, saves list of files and returns list of paths


class TaskCommentIssuesAttachmentView(APIView):
    permission_classes = [IsAuthenticated]


    def _process_media_urls(self, request, media_json):
        """Helper to process JSONB media fields into full URLs."""
        if not media_json:
            return []
        try:
            paths = json.loads(media_json) if isinstance(media_json, str) else media_json
            return [request.build_absolute_uri(settings.MEDIA_URL + path) for path in paths]
        except (json.JSONDecodeError, TypeError):
            return []

    def post(self, request):
        """
        Creates new task attachments (images or audio).
        - `images` field: multiple images
        - `audio` field: multiple audio
        Each file is stored as a separate row in taskCommentsIssues.
        """
        try:
            data = request.POST
            task_manager_id = to_int_or_none(data.get('taskManagerId'))
            employee_id = data.get('employeeId')
            employee_name = data.get('employeeName')
            is_deleted = to_int_or_none(data.get('isDeleted')) or 0

            inserted_ids = []

            # --- Handle Images ---
            image_files = request.FILES.getlist('images')
            if image_files:
                image_paths = save_files_task_comments_issues(
                    image_files,
                    task_manager_id if task_manager_id else 'attachments',
                    'images'
                )

                insert_query = """
                    INSERT INTO "taskCommentsIssues" (
                        "taskManagerId", "attachmentType", path, "employeeId",
                        "updatedAt", "isDeleted", "employeeName"
                    ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
                    RETURNING id;
                """
                for path in image_paths:
                    params = [task_manager_id, 0, json.dumps([path]), employee_id, is_deleted, employee_name]  # 0 = image
                    result = execute_query(insert_query, params, fetch='one')
                    if isinstance(result, dict):
                        inserted_ids.append(result.get("id"))
                    elif isinstance(result, list) and result:
                        inserted_ids.append(result[0].get("id"))

            # --- Handle Audio ---
            audio_files = request.FILES.getlist('audio')
            if audio_files:
                audio_paths = save_files_task_comments_issues(
                    audio_files,
                    task_manager_id if task_manager_id else 'attachments',
                    'audio'
                )

                insert_query = """
                    INSERT INTO "taskCommentsIssues" (
                        "taskManagerId", "attachmentType", path, "employeeId",
                        "updatedAt", "isDeleted", "employeeName"
                    ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
                    RETURNING id;
                """

                for path in audio_paths:
                    params = [task_manager_id, 1, json.dumps([path]), employee_id, is_deleted, employee_name]  # 1 = audio
                    result = execute_query(insert_query, params, fetch='one')
                    if isinstance(result, dict):
                        inserted_ids.append(result.get("id"))
                    elif isinstance(result, list) and result:
                        inserted_ids.append(result[0].get("id"))

            if not inserted_ids:
                return error_response(message="No files uploaded", status_code=status.HTTP_400_BAD_REQUEST)

            return success_response(
                data={"ids": inserted_ids},
                message="Comments added successfully",
                status_code=status.HTTP_201_CREATED
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error creating attachments: {str(e)}",
                                  status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    

    def put(self, request, attachment_id=None):
        """
        Updates an existing task attachment by ID.
        - Updates fields (taskManagerId, employeeId, etc.)
        - Can replace file for the given attachment_id
        - Can also insert new rows if multiple images/audio are uploaded
        """
        try:
            if attachment_id is None:
                return error_response(message="attachment_id required in URL", status_code=status.HTTP_400_BAD_REQUEST)

            attachment_id = to_int_or_none(attachment_id)
            if attachment_id is None:
                return error_response(message="Invalid attachment_id", status_code=status.HTTP_400_BAD_REQUEST)

            data = request.POST

            # Check if attachment exists
            check_query = 'SELECT * FROM "taskCommentsIssues" WHERE id = %s;'
            existing = execute_query(check_query, [attachment_id], fetch='one')
            if not existing:
                return error_response(message="Attachment not found", status_code=status.HTTP_404_NOT_FOUND)

            existing_dict = existing[0] if isinstance(existing, list) and existing else existing

            update_fields = []
            params = []

            # --- Metadata updates ---
            if 'taskManagerId' in data:
                update_fields.append('"taskManagerId" = %s')
                params.append(to_int_or_none(data['taskManagerId']))

            if 'employeeId' in data:
                update_fields.append('"employeeId" = %s')
                params.append(data['employeeId'])

            if 'isDeleted' in data:
                update_fields.append('"isDeleted" = %s')
                params.append(to_int_or_none(data['isDeleted']))

            # Always update updatedAt
            update_fields.append('"updatedAt" = CURRENT_TIMESTAMP')

            # Track updated/inserted IDs
            updated_ids = [attachment_id]

            # Common insert query for new files
            insert_query = """
                INSERT INTO "taskCommentsIssues" (
                    "taskManagerId", "attachmentType", path, "employeeId",
                    "updatedAt", "isDeleted"
                ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
                RETURNING id;
            """

            # --- Handle new images ---
            image_files = request.FILES.getlist('images')
            if image_files:
                image_paths = save_files_task_comments_issues(
                    image_files,
                    existing_dict.get('taskManagerId') or 'attachments',
                    'images'
                )
                for path in image_paths:
                    params_new = [
                        to_int_or_none(data.get('taskManagerId')) or existing_dict.get('taskManagerId'),
                        0,  # image
                        json.dumps([path]),
                        data.get('employeeId') or existing_dict.get('employeeId'),
                        to_int_or_none(data.get('isDeleted')) or existing_dict.get('isDeleted') or 0
                    ]
                    result = execute_query(insert_query, params_new, fetch='one')
                    if isinstance(result, dict):
                        updated_ids.append(result.get("id"))
                    elif isinstance(result, list) and result:
                        updated_ids.append(result[0].get("id"))

            # --- Handle new audio ---
            audio_files = request.FILES.getlist('audio')
            if audio_files:
                audio_paths = save_files_task_comments_issues(
                    audio_files,
                    existing_dict.get('taskManagerId') or 'attachments',
                    'audio'
                )
                for path in audio_paths:
                    params_new = [
                        to_int_or_none(data.get('taskManagerId')) or existing_dict.get('taskManagerId'),
                        1,  # audio
                        json.dumps([path]),
                        data.get('employeeId') or existing_dict.get('employeeId'),
                        to_int_or_none(data.get('isDeleted')) or existing_dict.get('isDeleted') or 0
                    ]
                    result = execute_query(insert_query, params_new, fetch='one')
                    if isinstance(result, dict):
                        updated_ids.append(result.get("id"))
                    elif isinstance(result, list) and result:
                        updated_ids.append(result[0].get("id"))

            # --- Update existing row metadata if needed ---
            if update_fields:
                update_query = 'UPDATE "taskCommentsIssues" SET ' + ', '.join(update_fields) + ' WHERE id = %s;'
                params.append(attachment_id)
                execute_query(update_query, params)

            return success_response(
                data={"ids": updated_ids},
                message="Comments updated successfully",
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error updating attachment: {str(e)}",
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



    def get(self, request, attachment_id=None):
        """
        Retrieves task attachments.
        - If attachment_id provided, get single by ID.
        - Else, list by query params (e.g., taskManagerId, attachmentType, isDeleted=0).
        """
        try:
            if attachment_id is not None:
                attachment_id = to_int_or_none(attachment_id)
                if attachment_id is None:
                    return error_response(message="Invalid attachment_id", status_code=status.HTTP_400_BAD_REQUEST)

                query = """
                    SELECT id, "taskManagerId", "attachmentType", path, "employeeId",
                        "createdAt", "updatedAt", "isDeleted", "employeeName"
                    FROM "taskCommentsIssues"
                    WHERE id = %s;
                """
                result = execute_query(query, [attachment_id], fetch='one')
                print(result)
                if not result:
                    return error_response(message="Attachment not found", status_code=status.HTTP_404_NOT_FOUND)
                
                # Process path into full URLs
                result['path'] = self._process_media_urls(request, result.get('path'))
                return success_response(data=result, message="Attachment retrieved successfully", status_code=status.HTTP_200_OK)

            # List mode
            task_manager_id = to_int_or_none(request.GET.get('taskManagerId'))
            attachment_type = to_int_or_none(request.GET.get('attachmentType'))
            is_deleted = to_int_or_none(request.GET.get('isDeleted', 0))  # Default to 0 (active)
            # employee_name = request.GET.get('employeeName')

            query = """
                SELECT id, "taskManagerId", "attachmentType", path, "employeeId",
                    "createdAt", "updatedAt", "isDeleted", "employeeName"
                FROM "taskCommentsIssues"
                WHERE ("isDeleted" = %s OR "isDeleted" IS NULL)
            """
            params = [is_deleted]

            if task_manager_id is not None:
                query += ' AND "taskManagerId" = %s'
                params.append(task_manager_id)

            if attachment_type is not None:
                query += ' AND "attachmentType" = %s'
                params.append(attachment_type)

            query += ' ORDER BY "createdAt" DESC;'

            results = execute_query(query, params, fetch='all')
            print(results)
            
            # Process paths into full URLs for all results
            if results:
                for result in results:
                    result['path'] = self._process_media_urls(request, result.get('path'))

            return success_response(data=results, message="Attachments retrieved successfully", status_code=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error retrieving attachments: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

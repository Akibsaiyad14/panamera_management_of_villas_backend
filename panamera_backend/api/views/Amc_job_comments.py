import json
import os
from django.db import IntegrityError
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response, save_job_files, log_activity_raw
from api.messages import *
from api.constants import *

class JobDayCommentView(APIView):
    """
    Handles GET (by date) and POST for the daily comment of a specific job.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, amcJobId):
        """
        GET /api/amcComments/{amcJobId}/
        Retrieves the latest comment for a specific job.
        """
        try:
            # Fetch the latest comment
            comment_query = 'SELECT * FROM "JobDayComments" WHERE "amcJobId" = %s ORDER BY "logDate" DESC LIMIT 1;'
            comments = execute_query(comment_query, [amcJobId], many=True)

            if not comments:
                return error_response(message=NO_COMMENTS_FOUND_FOR_JOB, status_code=status.HTTP_404_NOT_FOUND)

            comment = comments[0]

            # Build response object
            try:
                image_paths = json.loads(comment["images"]) if comment["images"] else []
                audio_paths = json.loads(comment["audio"]) if comment["audio"] else []
            except (TypeError, ValueError):
                image_paths = []
                audio_paths = []

            response_data = {
                "comment": comment["comment"],
                "logDate": comment["logDate"].strftime("%Y-%m-%d"),
                "imageUrls": [request.build_absolute_uri(f"/media/{path}") for path in image_paths],
                "audioUrls": [request.build_absolute_uri(f"/media/{path}") for path in audio_paths],
                "createdAt": comment["createdAt"].isoformat(),
                "updatedAt": comment["updatedAt"].isoformat() if comment["updatedAt"] else None,
            }

            return success_response(data=response_data, message=LATEST_COMMENT_FETCHED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def post(self, request, amcJobId):
        """
        POST /api/amcComments/{amcJobId}/
        Creates a new comment or updates an existing comment for a specific job, appending images and audio.
        """
        data = request.data
        try:
            if 'logDate' not in data:
                return error_response(message=LOGDATE_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)
            
            image_files = request.FILES.getlist('images')
            audio_files = request.FILES.getlist('audio')
            image_paths = save_job_files(image_files, 'comments', amcJobId)
            audio_paths = save_job_files(audio_files, 'comments', amcJobId)
            
            # Check if a comment already exists for the given amcJobId and logDate
            check_query = 'SELECT * FROM "JobDayComments" WHERE "amcJobId" = %s AND "logDate" = %s;'
            existing_comment_result = execute_query(check_query, [amcJobId, data['logDate']], many=False)

            # Handle case where execute_query returns a list
            existing_comment = existing_comment_result[0] if isinstance(existing_comment_result, list) and existing_comment_result else None

            if existing_comment:
                # Update existing comment by appending new images and audio
                existing_images = json.loads(existing_comment["images"]) if existing_comment["images"] else []
                existing_audio = json.loads(existing_comment["audio"]) if existing_comment["audio"] else []
                updated_images = existing_images + image_paths
                updated_audio = existing_audio + audio_paths

                update_query = """
                    UPDATE "JobDayComments"
                    SET "comment" = %s, "images" = %s, "audio" = %s, "updatedAt" = CURRENT_TIMESTAMP
                    WHERE "amcJobId" = %s AND "logDate" = %s
                    RETURNING *;
                """
                params = [
                    data.get('comment', existing_comment["comment"]),  # Keep existing comment if not provided
                    json.dumps(updated_images),
                    json.dumps(updated_audio),
                    amcJobId,
                    data['logDate']
                ]
                updated_comment_result = execute_query(update_query, params, many=False)
                
                # Handle case where execute_query returns a list
                updated_comment = updated_comment_result[0] if isinstance(updated_comment_result, list) and updated_comment_result else None

                if not updated_comment:
                    return error_response(message=FAILED_TO_UPDATE_COMMENT_RECORD, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

                response_data = {
                    "comment": updated_comment["comment"],
                    "logDate": updated_comment["logDate"].strftime("%Y-%m-%d"),
                    "imageUrls": json.loads(updated_comment["images"]) if updated_comment["images"] else [],
                    "audioUrls": json.loads(updated_comment["audio"]) if updated_comment["audio"] else [],
                    "createdAt": updated_comment["createdAt"].isoformat(),
                    "updatedAt": updated_comment["updatedAt"].isoformat() if updated_comment["updatedAt"] else None,
                }
                return success_response(
                    data=response_data,
                    message=COMMENT_UPDATED_SUCCESSFULLY,
                    status_code=status.HTTP_200_OK
                )
            else:
                # Create new comment
                insert_query = """
                    INSERT INTO "JobDayComments"
                    ("amcJobId", "logDate", "comment", "images", "audio", "createdBy")
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *;
                """
                params = [
                    amcJobId,
                    data['logDate'],
                    data.get('comment'),
                    json.dumps(image_paths),
                    json.dumps(audio_paths),
                    request.user.userName,
                ]
                new_comment_result = execute_query(insert_query, params, many=False)
                
                # Handle case where execute_query returns a list
                new_comment = new_comment_result[0] if isinstance(new_comment_result, list) and new_comment_result else None
                
                if not new_comment:
                    return error_response(message=FAILED_TO_CREATE_COMMENT_RECORD, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

                response_data = {
                    "comment": new_comment["comment"],
                    "logDate": new_comment["logDate"].strftime("%Y-%m-%d"),
                    "imageUrls": json.loads(new_comment["images"]) if new_comment["images"] else [],
                    "audioUrls": json.loads(new_comment["audio"]) if new_comment["audio"] else [],
                    "createdAt": new_comment["createdAt"].isoformat(),
                    "updatedAt": new_comment["updatedAt"].isoformat() if new_comment["updatedAt"] else None,
                }
                return success_response(
                    data=response_data,
                    message=COMMENT_CREATED_SUCCESSFULLY,
                    status_code=status.HTTP_201_CREATED
                )

        except IntegrityError:
            return error_response(message=COMMENT_ALREADY_EXISTS_FOR_JOB_AND_DATE, status_code=status.HTTP_409_CONFLICT)
        except Exception as e:
            return error_response(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, amcJobId):
        """
        PUT /api/amcComments/{amcJobId}
        Clears images for a specific comment and deletes image files from media folder.
        Required body: {"commentId": <id>}
        """
        try:
            data = request.data
            
            fetch_query = '''
                SELECT  c."amcJobId", c."images", j."amcJobName", j."visitType"
                FROM "JobDayComments" AS c
                LEFT JOIN "AmcJobs" AS j ON c."amcJobId" = j."amcJobId"
                WHERE  c."amcJobId" = %s
                LIMIT 1;
            '''
            comment_result = execute_query(fetch_query, [amcJobId], many=False)
            comment = comment_result[0] if isinstance(comment_result, list) and comment_result else comment_result

            if not comment:
                return error_response(message="Comment not found", status_code=status.HTTP_404_NOT_FOUND)

            try:
                image_paths = json.loads(comment.get("images")) if comment.get("images") else []
            except (TypeError, ValueError):
                image_paths = []

            deleted_files = []
            missing_files = []
            job_comments_dir = os.path.abspath(os.path.join(settings.MEDIA_ROOT, 'comments', str(amcJobId)))
            # folders_to_remove = set()
            # print(f"Image paths to process: {image_paths}")

            for relative_path in image_paths:
                if not relative_path:
                    continue

                # ✅ Only images
                if not relative_path.lower().endswith(IMAGE_EXTENSIONS):
                    continue

                abs_file_path = os.path.abspath(
                    os.path.join(settings.MEDIA_ROOT, relative_path)
                )

                print(f"Processing file: {abs_file_path}")

                # 🔒 CRITICAL: Ensure file belongs to THIS amcJobId folder only
                if not abs_file_path.startswith(job_comments_dir + os.sep):
                    print(f"❌ Skipping чужой/invalid path: {abs_file_path}")
                    continue

                if os.path.isfile(abs_file_path):
                    os.remove(abs_file_path)
                    deleted_files.append(relative_path)
                else:
                    missing_files.append(relative_path)

            # Remove the folders that contain the images directly.
            deleted_folders = []

            if os.path.isdir(job_comments_dir):
                for root, dirs, files in os.walk(job_comments_dir, topdown=False):
                    if not os.listdir(root):  # folder is empty
                        os.rmdir(root)
                        deleted_folders.append(root)

            update_query = '''
                UPDATE "JobDayComments"
                SET "images" = %s, "updatedAt" = CURRENT_TIMESTAMP
                WHERE "amcJobId" = %s
                RETURNING "commentId", "amcJobId", "images", "updatedAt";
            '''
            updated_result = execute_query(update_query, [json.dumps([]), amcJobId], many=False)
            updated_comment = updated_result[0] if isinstance(updated_result, list) and updated_result else updated_result

            if not updated_comment:
                return error_response(message=FAILED_TO_UPDATE_COMMENT_RECORD, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            amc_types = comment.get("visitType") or "N/A"
            if amc_types == 1:
                amc_types = GARDEN_VISIT
            else:
                amc_types = POOL_VISIT

            log_activity_raw(
                request,
                category="AmcJobComment",
                action="ClearImages",
                performer=request.user,
                details={
                    "amcJobId": amcJobId,
                    "amcJobName": comment.get("amcJobName"),
                    "visitType": amc_types,
                }
            )

            return success_response(
                data={
                    "amcJobId": updated_comment.get("amcJobId"),
                    "imageUrls": [],
                    "deletedFiles": deleted_files,
                    "missingFiles": missing_files,
                    "deletedFolders": deleted_folders,
                    "updatedAt": updated_comment["updatedAt"].isoformat() if updated_comment.get("updatedAt") else None,
                },
                message=COMMENT_IMAGES_CLEARED_SUCCESSFULLY,
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

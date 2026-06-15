from api.messages import *
import os
import json
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from django.core.files.storage import default_storage
from api.utils import success_response, error_response, execute_query, delete_old_overtime_folders, save_overtime_files


class UploadOvertimeMediaView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            overtime_request_id = request.data.get("attendanceRecordId")
            overtime_reason = request.data.get("reasonText", "")
            photo_files = request.FILES.getlist("photoFiles")
            voice_files = request.FILES.getlist("voiceFiles")

            if not overtime_request_id:
                return error_response(
                    message="overtimeRequestId is required.",
                    status_code=drf_status.HTTP_400_BAD_REQUEST
                )

            record_exists = execute_query(
                """SELECT "attendanceRecordId" FROM "overtimeRequests" WHERE "attendanceRecordId" = %s AND "isDeleted" = 0""",
                [overtime_request_id],
                many=False
            )

            if not record_exists:
                return error_response(
                    message="Overtime request not found.",
                    status_code=drf_status.HTTP_404_NOT_FOUND
                )

            # Clean old folders for this attendanceId
            delete_old_overtime_folders(overtime_request_id, days=7)

            # Save new files
            photo_paths = save_overtime_files(overtime_request_id, photo_files)
            voice_paths = save_overtime_files(overtime_request_id, voice_files)

            execute_query(
                """
                UPDATE "overtimeRequests"
                SET
                    "reasonPhotoPath" = %s,
                    "reasonVoiceNotePath" = %s,
                    "reasonText" = %s,
                    "updatedAt" = NOW()
                WHERE "attendanceRecordId" = %s
                """,
                [json.dumps(photo_paths), json.dumps(voice_paths), overtime_reason, overtime_request_id],
                fetch=False
            )

            return success_response(
                message="Overtime Details are saved successfully.",
                data={
                    "photoPaths": photo_paths,
                    "voiceNotePaths": voice_paths
                },
                status_code=200
            )

        except Exception as e:
            return error_response(
                message=f"Error uploading media files: {str(e)}",
                status_code=500
            )
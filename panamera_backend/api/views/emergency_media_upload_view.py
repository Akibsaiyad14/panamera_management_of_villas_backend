from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from datetime import datetime
from rest_framework import status as http_status
import json
from ..utils import error_response, success_response, execute_query, log_activity_raw, save_emergency_files
from ..messages import (
    MISSING_REQUIRED_FIELDS,
    INVALID_FORMAT_FOR_INPUTS,
    NO_EMERGENCY_CHECKIN_FOUND,
    ALREADY_EMERGENCY_CHECKED_OUT
)


class EmergencyMediaUploadView(APIView):
    """
    Handles uploading emergency reason, images and audio after emergency check-out is completed.
    Media can only be uploaded after both emergency check-in and check-out are done.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        user_id = request.user.id

        try:
            attendance_id = request.data.get("attendanceId")
            emergency_reason = request.data.get("emergencyReason", "")
            is_offline = str(request.data.get("isOfflineData", "false")).lower() == "true"
            
            # Handle images and audio files
            image_files = request.FILES.getlist('images', [])
            audio_files = request.FILES.getlist('audio', [])

            # Validation
            if not attendance_id:
                return error_response(
                    message="attendanceId is required",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            # At least emergency reason or media files required
            if not emergency_reason and not image_files and not audio_files:
                return error_response(
                    message="Emergency reason or at least one image/audio file is required",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

        except (ValueError, TypeError, AttributeError) as e:
            return error_response(
                message=INVALID_FORMAT_FOR_INPUTS + f" Error: {e}",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )

        # Fetch attendance record by ID
        attendance_result = execute_query("""
            SELECT id, date, "labourUserId", "checkInTime", "checkOutTime", 
                   "emergencyCheckInTime", "emergencyCheckOutTime", 
                   "emergencyCheckOutImages", "emergencyCheckOutAudio", 
                   "assignedShiftAtCheckInId"
            FROM attendance
            WHERE id = %s AND "isDeleted" = 0
        """, [attendance_id], many=False)

        if not attendance_result:
            return error_response(
                message="No attendance record found for the specified attendanceId.",
                status_code=http_status.HTTP_404_NOT_FOUND
            )

        record = attendance_result[0]

        # Verify the attendance belongs to the authenticated user
        if record.get('labourUserId') != user_id:
            return error_response(
                message="You are not authorized to upload media for this attendance record.",
                status_code=http_status.HTTP_403_FORBIDDEN
            )

        # Validate that both emergency check-in and check-out exist
        if not record.get('emergencyCheckInTime'):
            return error_response(
                message=NO_EMERGENCY_CHECKIN_FOUND + " Please complete emergency check-in first.",
                status_code=http_status.HTTP_400_BAD_REQUEST
            )
        
        # if not record.get('emergencyCheckOutTime'):
        #     return error_response(
        #         message="Emergency check-out not completed yet. Please complete emergency check-out before uploading media.",
        #         status_code=http_status.HTTP_400_BAD_REQUEST
        #     )

        # Handle offline sync - check if media already uploaded
        if is_offline:
            existing_images = json.loads(record.get('emergencyCheckOutImages') or '[]')
            existing_audio = json.loads(record.get('emergencyCheckOutAudio') or '[]')
            existing_reason = record.get('emergencyReason')
            
            # If data already exists and no new files, skip (already synced)
            if (existing_reason or existing_images or existing_audio) and not image_files and not audio_files:
                return success_response(
                    data={
                        'attendanceId': record['id'],
                        'emergencyCheckInTime': record['emergencyCheckInTime'].strftime("%Y-%m-%d %H:%M:%S"),
                        'emergencyCheckOutTime': record['emergencyCheckOutTime'].strftime("%Y-%m-%d %H:%M:%S") if record['emergencyCheckOutTime'] else None,
                        'emergencyReason': existing_reason,
                        'totalImages': len(existing_images),
                        'totalAudio': len(existing_audio),
                        'allImages': existing_images,
                        'allAudio': existing_audio
                    },
                    message="Emergency media already synced",
                    status_code=http_status.HTTP_200_OK
                )
        
        # Save files
        image_paths = save_emergency_files(record['id'], image_files, 'checkout_images') if image_files else []
        audio_paths = save_emergency_files(record['id'], audio_files, 'checkout_audio') if audio_files else []
        
        # Get existing media
        existing_images = json.loads(record.get('emergencyCheckOutImages') or '[]')
        existing_audio = json.loads(record.get('emergencyCheckOutAudio') or '[]')
        
        # Merge with new uploads
        all_images = existing_images + image_paths
        all_audio = existing_audio + audio_paths
        
        # Update database with emergency reason and media
        execute_query("""
            UPDATE attendance
            SET "emergencyReason" = %s,
                "emergencyCheckOutImages" = %s,
                "emergencyCheckOutAudio" = %s,
                "updatedAt" = NOW()
            WHERE id = %s
        """, [emergency_reason, json.dumps(all_images), json.dumps(all_audio), record['id']])

        log_activity_raw(
            request=request,
            category='Attendance',
            action='OfflineEmergencyMediaUpload' if is_offline else 'EmergencyMediaUpload',
            performer=user,
            target_employee_name=getattr(user, 'fullName', None),
            target_shift_id=record.get('assignedShiftAtCheckInId'),
            details={
                'attendanceId': record['id'],
                'emergencyReason': emergency_reason,
                'newImages': image_paths,
                'newAudio': audio_paths,
                'totalImages': len(all_images),
                'totalAudio': len(all_audio),
                'isOfflineSync': is_offline
            }
        )

        success_message = "Emergency data synced successfully" if is_offline else "Emergency data uploaded successfully"
        
        return success_response(
            data={
                'attendanceId': record['id'],
                'emergencyCheckInTime': record['emergencyCheckInTime'].strftime("%Y-%m-%d %H:%M:%S"),
                'emergencyCheckOutTime':record['emergencyCheckOutTime'].strftime("%Y-%m-%d %H:%M:%S") if record['emergencyCheckOutTime'] else None,
                'emergencyReason': emergency_reason,
                'newImagesUploaded': len(image_paths),
                'newAudioUploaded': len(audio_paths),
                'totalImages': len(all_images),
                'totalAudio': len(all_audio),
                'allImages': all_images,
                'allAudio': all_audio
            },
            message=success_message,
            status_code=http_status.HTTP_200_OK
        )

"""
Celery tasks for Leave Application management
Handles asynchronous operations like certificate deadline checks and notifications
"""

from celery import shared_task
from datetime import datetime
import traceback
from api.utils import execute_query, _send_notification, log_activity_raw
from api.constants import LEAVE_TYPE_SICK, LEAVE_STATUS_REJECTED
from api.messages import (
    LEAVE_AUTO_REJECTED_CERTIFICATE_MISSING,
    LEAVE_AUTO_REJECTED_NOTIFICATION_TITLE,
    LEAVE_AUTO_REJECTED_NOTIFICATION_BODY,
    LEAVE_AUTO_REJECTED_SUPERVISOR_TITLE,
    LEAVE_AUTO_REJECTED_SUPERVISOR_BODY
)


@shared_task(name='api.tasks.check_leave_certificate_deadline', bind=True, max_retries=3)
def check_leave_certificate_deadline(self, leave_id):
    """
    Check if a sick leave certificate has been uploaded within the deadline.
    This task is scheduled to run at the configured deadline time after a sick leave is created.
    (Deadline configurable via LEAVE_CERTIFICATE_DEADLINE_HOURS constant)
    
    Args:
        leave_id (int): ID of the leave application to check
    
    Returns:
        dict: Result of the check with status and actions taken
    """
    try:
        print(f"[CELERY TASK] Checking certificate deadline for leave ID: {leave_id}")
        
        # Fetch the leave application details
        query = """
            SELECT 
                la.id, 
                la."leaveId", 
                la."employeeId", 
                la."leaveType",
                la."leaveCertificate",
                la."certificateUploadDeadline",
                la."leaveStatus",
                la."isDeleted",
                la."startDate",
                la."endDate",
                u.id AS "userId",
                u."fcmToken",
                u."fullName",
                u."phoneNumber"
            FROM "leaveApplication" la
            LEFT JOIN "user" u ON la."employeeId" = u."employeeId" AND COALESCE(u."isDeleted", '0') = '0'
            WHERE la.id = %s AND COALESCE(la."isDeleted", 0) = 0
        """
        
        result = execute_query(query, [leave_id], fetch='one')
        
        if isinstance(result, list) and result:
            leave_data = result[0]
        else:
            leave_data = result
        
        if not leave_data:
            print(f"[CELERY TASK] Leave application {leave_id} not found or deleted")
            return {
                'status': 'not_found',
                'leave_id': leave_id,
                'message': 'Leave application not found or has been deleted'
            }
        
        # Validate it's a sick leave
        if leave_data.get('leaveType') != LEAVE_TYPE_SICK:
            print(f"[CELERY TASK] Leave {leave_id} is not a sick leave, skipping")
            return {
                'status': 'skipped',
                'leave_id': leave_id,
                'message': 'Not a sick leave, no certificate required'
            }
        
        # Check if leave is still pending/approved (not rejected)
        if leave_data.get('leaveStatus') == LEAVE_STATUS_REJECTED:
            print(f"[CELERY TASK] Leave {leave_id} was rejected, skipping certificate check")
            return {
                'status': 'skipped',
                'leave_id': leave_id,
                'message': 'Leave was rejected, certificate check not needed'
            }
        
        # Check if certificate was uploaded
        has_certificate = leave_data.get('leaveCertificate') is not None and leave_data.get('leaveCertificate') != ''
        
        if has_certificate:
            print(f"[CELERY TASK] Certificate already uploaded for leave {leave_id}")
            return {
                'status': 'success',
                'leave_id': leave_id,
                'message': 'Certificate uploaded before deadline',
                'certificate': leave_data.get('leaveCertificate')
            }
        
        # Certificate not uploaded - deadline has passed - REJECT THE LEAVE
        print(f"[CELERY TASK] Certificate NOT uploaded for leave {leave_id} - AUTO-REJECTING LEAVE!")
        
        # Update leave application: Mark as overdue AND reject the leave
        rejection_reason = LEAVE_AUTO_REJECTED_CERTIFICATE_MISSING
        update_query = """
            UPDATE "leaveApplication" 
            SET "certificateOverdue" = TRUE,
                "certificateOverdueAt" = %s,
                "leaveStatus" = %s,
                "rejectedBy" = %s,
                "rejectedAt" = %s,
                "rejectionReason" = %s,
                "updatedAt" = %s
            WHERE id = %s
        """
        current_time = datetime.now()
        execute_query(update_query, [
            current_time,                    # certificateOverdueAt
            LEAVE_STATUS_REJECTED,           # leaveStatus = 4 (Rejected)
            'SYSTEM',                        # rejectedBy (system auto-rejection)
            current_time,                    # rejectedAt
            rejection_reason,                # rejectionReason
            current_time,                    # updatedAt
            leave_id                         # WHERE id
        ])

        log_activity_raw(
            request=None,
            category='Leave',
            action='AutoRejectCertificate',
            performer=None,
            target_employee_name=leave_data.get('fullName'),
            details={
                'leaveId': leave_data.get('leaveId'),
                'leaveType': leave_data.get('leaveType'),
                'rejectionReason': rejection_reason
            }
        )
        
        print(f"[CELERY TASK] Leave {leave_id} has been REJECTED due to missing certificate")
        
        # Send notification to employee about leave rejection
        try:
            if leave_data.get('userId') and leave_data.get('fcmToken'):
                title = LEAVE_AUTO_REJECTED_NOTIFICATION_TITLE
                body = LEAVE_AUTO_REJECTED_NOTIFICATION_BODY.format(
                    leave_id=leave_data.get('leaveId')
                )
                
                _send_notification(
                    recipient_user_id=leave_data.get('userId'),
                    title=title,
                    body=body,
                    notification_type="LEAVE_AUTO_REJECTED",
                    data_payload={
                        "leaveId": str(leave_id),
                        "type": "LEAVE_AUTO_REJECTED",
                        "reason": "certificate_not_uploaded"
                    },
                    fcm_token=leave_data.get('fcmToken')
                )
                print(f"[CELERY TASK] Rejection notification sent to employee for leave {leave_id}")
        except Exception as notify_error:
            print(f"[CELERY TASK] Failed to send rejection notification: {str(notify_error)}")
        
        # Notify supervisor/HR about leave auto-rejection
        # Skip if employee is their own supervisor (e.g., Team Leader applying for their own leave)
        try:
            # Get supervisor/team leader
            supervisor_query = """
                SELECT 
                    supervisor.id AS "supervisorId",
                    supervisor."fcmToken" AS "supervisorFcmToken",
                    supervisor."fullName" AS "supervisorName"
                FROM "user" u
                LEFT JOIN "user" supervisor ON u."reportingToId" = supervisor.id 
                    AND COALESCE(supervisor."isDeleted", 0) = 0
                WHERE u."employeeId" = %s
            """
            supervisor_result = execute_query(
                supervisor_query, 
                [leave_data.get('employeeId')], 
                fetch='one'
            )
            
            if supervisor_result:
                if isinstance(supervisor_result, list):
                    supervisor_data = supervisor_result[0]
                else:
                    supervisor_data = supervisor_result
                
                supervisor_id = supervisor_data.get('supervisorId')
                employee_user_id = leave_data.get('userId')
                
                # Check if supervisor is different from employee (avoid duplicate notifications)
                if supervisor_id and supervisor_data.get('supervisorFcmToken') and supervisor_id != employee_user_id:
                    title = LEAVE_AUTO_REJECTED_SUPERVISOR_TITLE
                    body = LEAVE_AUTO_REJECTED_SUPERVISOR_BODY.format(
                        employee_name=leave_data.get('fullName'),
                        leave_id=leave_data.get('leaveId'),
                        start_date=leave_data.get('startDate'),
                        end_date=leave_data.get('endDate')
                    )
                    
                    _send_notification(
                        recipient_user_id=supervisor_id,
                        title=title,
                        body=body,
                        notification_type="LEAVE_AUTO_REJECTED_SUPERVISOR",
                        data_payload={
                            "leaveId": str(leave_id),
                            "type": "LEAVE_AUTO_REJECTED_SUPERVISOR",
                            "employeeId": leave_data.get('employeeId'),
                            "reason": "certificate_not_uploaded"
                        },
                        fcm_token=supervisor_data.get('supervisorFcmToken')
                    )
                    print(f"[CELERY TASK] Rejection notification sent to supervisor for leave {leave_id}")
                elif supervisor_id == employee_user_id:
                    print(f"[CELERY TASK] Skipped supervisor notification for leave {leave_id} (employee is their own supervisor)")
                else:
                    print(f"[CELERY TASK] No supervisor found or FCM token missing for leave {leave_id}")
        except Exception as supervisor_notify_error:
            print(f"[CELERY TASK] Failed to send supervisor rejection notification: {str(supervisor_notify_error)}")
        
        return {
            'status': 'leave_rejected',
            'leave_id': leave_id,
            'leave_number': leave_data.get('leaveId'),
            'employee': leave_data.get('fullName'),
            'message': 'Leave automatically rejected - certificate not uploaded within 12 hours',
            'rejection_reason': rejection_reason,
            'notifications_sent': True
        }
    
    except Exception as e:
        traceback.print_exc()
        print(f"[CELERY TASK ERROR] Failed to check certificate for leave {leave_id}: {str(e)}")
        
        # Retry the task with exponential backoff
        try:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1))
        except self.MaxRetriesExceededError:
            return {
                'status': 'error',
                'leave_id': leave_id,
                'message': f'Max retries exceeded: {str(e)}'
            }


@shared_task(name='api.tasks.cleanup_expired_leave_tasks')
def cleanup_expired_leave_tasks():
    """
    Periodic task to clean up old task records and perform maintenance.
    Runs once per day via Celery Beat.
    
    This can be expanded to:
    - Remove old periodic task entries
    - Clean up task result history
    - Archive old leave records
    """
    try:
        print("[CELERY BEAT] Running daily cleanup task...")
        
        # Example: Find and log leaves with expired deadlines that weren't checked
        query = """
            SELECT COUNT(*) as count
            FROM "leaveApplication"
            WHERE "leaveType" = %s 
            AND "certificateUploadDeadline" < %s
            AND "leaveCertificate" IS NULL
            AND COALESCE("certificateOverdue", FALSE) = FALSE
            AND COALESCE("isDeleted", 0) = 0
        """
        
        result = execute_query(
            query, 
            [LEAVE_TYPE_SICK, datetime.now()], 
            fetch='one'
        )
        
        if result:
            count = result[0].get('count', 0) if isinstance(result, list) else result.get('count', 0)
            print(f"[CELERY BEAT] Found {count} leaves with missed deadline checks")
        
        return {
            'status': 'success',
            'message': 'Cleanup completed',
            'timestamp': datetime.now().isoformat()
        }
    
    except Exception as e:
        traceback.print_exc()
        print(f"[CELERY BEAT ERROR] Cleanup task failed: {str(e)}")
        return {
            'status': 'error',
            'message': str(e)
        }


@shared_task(name='api.tasks.test_celery')
def test_celery():
    """Simple test task to verify Celery is working"""
    print("[CELERY TEST] Test task executed successfully!")
    return {
        'status': 'success',
        'message': 'Celery is working correctly',
        'timestamp': datetime.now().isoformat()
    }

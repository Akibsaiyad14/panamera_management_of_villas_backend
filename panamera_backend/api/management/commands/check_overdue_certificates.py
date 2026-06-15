"""
Django management command to check and auto-reject leaves with overdue certificates
This runs as a backup mechanism in case Celery tasks fail

Usage: python manage.py check_overdue_certificates
Recommended cron: */15 * * * * (every 15 minutes)
"""

from django.core.management.base import BaseCommand
from datetime import datetime
from api.utils import execute_query, _send_notification, log_activity_raw
from api.constants import LEAVE_TYPE_SICK, LEAVE_STATUS_REJECTED, LEAVE_STATUS_HR_APPROVED
from api.messages import (
    LEAVE_AUTO_REJECTED_CERTIFICATE_MISSING,
    LEAVE_AUTO_REJECTED_NOTIFICATION_TITLE,
    LEAVE_AUTO_REJECTED_NOTIFICATION_BODY,
    LEAVE_AUTO_REJECTED_SUPERVISOR_TITLE,
    LEAVE_AUTO_REJECTED_SUPERVISOR_BODY
)


class Command(BaseCommand):
    help = 'Check for sick leaves with expired certificate deadlines and auto-reject them (backup for Celery)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be rejected without actually rejecting',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed output',
        )
        parser.add_argument(
            '--check-celery',
            action='store_true',
            help='Only run if Celery is not working (smart mode)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force run even if Celery is working',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        verbose = options['verbose']
        check_celery = options['check_celery']
        force = options['force']
        
        # Smart mode: Check if Celery is working before running
        if check_celery and not force:
            celery_healthy = self._check_celery_health()
            if celery_healthy:
                if verbose:
                    self.stdout.write(self.style.SUCCESS('✓ Celery is healthy - skipping cron check'))
                    self.stdout.write('  (Celery will handle certificate checks automatically)')
                return
            else:
                if verbose:
                    self.stdout.write(self.style.WARNING('⚠ Celery is not healthy - proceeding with cron check'))
                    self.stdout.write('')
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
            self.stdout.write('')
        
        current_time = datetime.now()
        
        # Find sick leaves where:
        # 1. Certificate deadline has passed
        # 2. No certificate uploaded
        # 3. Leave not yet rejected
        # 4. Leave not deleted
        query = """
            SELECT 
                la.id, 
                la."leaveId", 
                la."employeeId", 
                la."leaveType",
                la."leaveCertificate",
                la."certificateUploadDeadline",
                la."leaveStatus",
                la."startDate",
                la."endDate",
                u.id AS "userId",
                u."fcmToken",
                u."fullName",
                u."reportingToId"
            FROM "leaveApplication" la
            LEFT JOIN "user" u ON la."employeeId" = u."employeeId" AND COALESCE(u."isDeleted", '0') = '0'
            WHERE la."leaveType" = %s
                AND la."certificateUploadDeadline" IS NOT NULL
                AND la."certificateUploadDeadline" < %s
                AND (la."leaveCertificate" IS NULL OR la."leaveCertificate" = '')
                AND la."leaveStatus" != %s
                AND COALESCE(la."isDeleted", 0) = 0
                AND COALESCE(la."certificateOverdue", FALSE) = FALSE
            ORDER BY la."certificateUploadDeadline" ASC
        """
        
        overdue_leaves = execute_query(
            query, 
            [LEAVE_TYPE_SICK, current_time, LEAVE_STATUS_REJECTED],
            fetch=True
        )
        
        if not overdue_leaves:
            if verbose:
                self.stdout.write(self.style.SUCCESS('✓ No overdue certificates found - all good!'))
            return
        
        self.stdout.write(self.style.WARNING(f'Found {len(overdue_leaves)} leave(s) with overdue certificates'))
        self.stdout.write('')
        
        rejected_count = 0
        failed_count = 0
        
        for leave in overdue_leaves:
            leave_id = leave.get('id')
            leave_number = leave.get('leaveId')
            employee_name = leave.get('fullName', 'Unknown')
            deadline = leave.get('certificateUploadDeadline')
            
            hours_overdue = (current_time - deadline).total_seconds() / 3600 if deadline else 0
            
            self.stdout.write(f"Leave {leave_number} (ID: {leave_id})")
            self.stdout.write(f"  Employee: {employee_name}")
            self.stdout.write(f"  Deadline: {deadline}")
            self.stdout.write(f"  Overdue by: {hours_overdue:.1f} hours")
            
            if dry_run:
                self.stdout.write(self.style.WARNING('  → Would be REJECTED (dry-run mode)'))
                self.stdout.write('')
                continue
            
            try:
                # Reject the leave
                success = self._reject_leave_for_missing_certificate(leave, current_time)
                
                if success:
                    rejected_count += 1
                    self.stdout.write(self.style.ERROR(f"  → REJECTED (certificate not uploaded)"))
                    
                    # Send notifications
                    self._send_rejection_notifications(leave)
                    self.stdout.write(self.style.SUCCESS(f"  → Notifications sent"))
                else:
                    failed_count += 1
                    self.stdout.write(self.style.ERROR(f"  → FAILED to reject"))
                    
            except Exception as e:
                failed_count += 1
                self.stdout.write(self.style.ERROR(f"  → ERROR: {str(e)}"))
            
            self.stdout.write('')
        
        # Summary
        self.stdout.write('=' * 70)
        if dry_run:
            self.stdout.write(self.style.WARNING(f'DRY RUN: {len(overdue_leaves)} leave(s) would be rejected'))
        else:
            self.stdout.write(self.style.SUCCESS(f'✓ Successfully rejected: {rejected_count}'))
            if failed_count > 0:
                self.stdout.write(self.style.ERROR(f'✗ Failed to reject: {failed_count}'))
        self.stdout.write('=' * 70)
    
    def _reject_leave_for_missing_certificate(self, leave, current_time):
        """
        Reject a leave application for missing certificate
        Returns True if successful, False otherwise
        """
        leave_id = leave.get('id')
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
        
        try:
            execute_query(update_query, [
                current_time,                    # certificateOverdueAt
                LEAVE_STATUS_REJECTED,           # leaveStatus = 4
                'SYSTEM_CRON',                   # rejectedBy (cron auto-rejection)
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
                target_employee_name=leave.get('fullName'),
                details={
                    'leaveId': leave.get('leaveId'),
                    'leaveType': leave.get('leaveType'),
                    'rejectionReason': rejection_reason
                }
            )
            return True
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Database error: {str(e)}"))
            return False
    
    def _send_rejection_notifications(self, leave):
        """
        Send notifications to employee and supervisor about leave rejection
        """
        leave_id = leave.get('id')
        leave_number = leave.get('leaveId')
        employee_name = leave.get('fullName')
        user_id = leave.get('userId')
        fcm_token = leave.get('fcmToken')
        start_date = leave.get('startDate')
        end_date = leave.get('endDate')
        
        # Notify employee
        try:
            if user_id and fcm_token:
                title = LEAVE_AUTO_REJECTED_NOTIFICATION_TITLE
                body = LEAVE_AUTO_REJECTED_NOTIFICATION_BODY.format(leave_id=leave_number)
                
                _send_notification(
                    recipient_user_id=user_id,
                    title=title,
                    body=body,
                    notification_type="LEAVE_AUTO_REJECTED",
                    data_payload={
                        "leaveId": str(leave_id),
                        "type": "LEAVE_AUTO_REJECTED",
                        "reason": "certificate_not_uploaded"
                    },
                    fcm_token=fcm_token
                )
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  ⚠ Failed to notify employee: {str(e)}"))
        
        # Notify supervisor
        try:
            reporting_to_id = leave.get('reportingToId')
            if reporting_to_id:
                supervisor_query = """
                    SELECT id, "fcmToken", "fullName"
                    FROM "user"
                    WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
                """
                supervisor_result = execute_query(supervisor_query, [reporting_to_id], fetch='one')
                
                if supervisor_result:
                    if isinstance(supervisor_result, list):
                        supervisor_data = supervisor_result[0]
                    else:
                        supervisor_data = supervisor_result
                    
                    if supervisor_data.get('fcmToken'):
                        title = LEAVE_AUTO_REJECTED_SUPERVISOR_TITLE
                        body = LEAVE_AUTO_REJECTED_SUPERVISOR_BODY.format(
                            employee_name=employee_name,
                            leave_id=leave_number,
                            start_date=start_date,
                            end_date=end_date
                        )
                        
                        _send_notification(
                            recipient_user_id=supervisor_data.get('id'),
                            title=title,
                            body=body,
                            notification_type="LEAVE_AUTO_REJECTED_SUPERVISOR",
                            data_payload={
                                "leaveId": str(leave_id),
                                "type": "LEAVE_AUTO_REJECTED_SUPERVISOR",
                                "employeeId": leave.get('employeeId'),
                                "reason": "certificate_not_uploaded"
                            },
                            fcm_token=supervisor_data.get('fcmToken')
                        )
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  ⚠ Failed to notify supervisor: {str(e)}"))
    
    def _check_celery_health(self):
        """
        Check if Celery and Redis are working
        Returns True if healthy, False otherwise
        """
        try:
            from api.celery_health import check_redis_connection, check_celery_workers
            
            # Check Redis
            redis_status = check_redis_connection()
            if redis_status.get('status') != 'healthy':
                return False
            
            # Check Celery workers
            celery_status = check_celery_workers()
            if not celery_status.get('workers_running'):
                return False
            
            return True
        except Exception as e:
            # If health check fails, assume unhealthy (safer to run cron)
            self.stdout.write(self.style.WARNING(f'Health check error: {str(e)}'))
            return False

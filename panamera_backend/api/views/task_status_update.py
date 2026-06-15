from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, log_activity_raw, _send_notification, _send_email, send_mail_with_template
from api.messages import *
import datetime
import json
from dateutil.relativedelta import relativedelta
from api.constants import *
import logging
from django.utils import timezone

# It's good practice to use a logger for info/warnings
logger = logging.getLogger(__name__)


class TaskStatusUpdateView(APIView):
    """
    API endpoint to update the status of a single task/issue.
    - If a customer request is closed, notifies the Customer (push & email).
    - If a non-request is closed by a Team Leader, notifies the Supervisor (push) and a central Office Admin (email).
    """
    permission_classes = [IsAuthenticated]

    def put(self, request, taskk_id=None):
        try:
            # --- 1. Validate Input (Unchanged) ---
            if not taskk_id: return error_response(message="Task id is required.", status_code=status.HTTP_400_BAD_REQUEST)
            data = request.data
            new_status = data.get('status')
            if new_status is None: return error_response(message="'status' field is required.", status_code=status.HTTP_400_BAD_REQUEST)
            try:
                new_status = int(new_status)
                if new_status not in [OPEN, CLOSED, ON_HOLD, IN_PROGRESS, AWAITING_GATE_PASS, QUOTATION_STAGE, JOB_APPROVED, CANCELLED]: raise ValueError
            except (ValueError, TypeError):
                return error_response(message="Invalid 'status' value.", status_code=status.HTTP_400_BAD_REQUEST)

            # --- 2. Fetch Current Task State (Unchanged) ---
            fetch_query = """
                SELECT "taskName", "taskStatus", "customerId", "supervisorId", "wasRequested", "lastStatusDate"
                FROM "taskManager" WHERE id = %s AND COALESCE("isDeleted", 0) = 0
            """
            current_task_result = execute_query(fetch_query, [taskk_id], fetch=True)
            if not current_task_result:
                return error_response(message=f"No active task found with ID {taskk_id}.", status_code=status.HTTP_404_NOT_FOUND)
            
            current_task = current_task_result[0]
            task_name, old_status_numeric = current_task['taskName'], current_task['taskStatus']
            customer_business_id, supervisor_employee_id = current_task.get('customerId'), current_task.get('supervisorId')
            was_requested = current_task.get('wasRequested', False)
            current_time = timezone.now()
            last_status_date = current_time
            update_query = 'UPDATE "taskManager" SET "taskStatus" = %s, "lastStatusDate" = %s, "updatedAt" = %s WHERE id = %s;'
            execute_query(update_query, [new_status, last_status_date, current_time, taskk_id])

            # --- 4. Log the Activity (Unchanged) ---
            STATUS_MAP = {OPEN: 'Open', CLOSED: 'Closed', ON_HOLD: 'On Hold', IN_PROGRESS: 'In Progress', AWAITING_GATE_PASS: 'Awaiting Gate Pass', QUOTATION_STAGE: 'Quotation Stage', JOB_APPROVED: 'Job Approved', CANCELLED: 'Cancelled'}
            old_status_str, new_status_str = STATUS_MAP.get(old_status_numeric, str(old_status_numeric)), STATUS_MAP.get(new_status, str(new_status))
            log_activity_raw(request=request, category='Task', action='StatusChange', performer=request.user, details={'id': taskk_id, 'title': task_name, 'oldStatus': old_status_str, 'newStatus': new_status_str})
            
            # =================================================================
            # --- 5. NOTIFICATION LOGIC ---
            # =================================================================
            
            if new_status == CLOSED and old_status_numeric != CLOSED:
                
                # --- PATH 1: It was a customer request ---
                if was_requested:
                    # ... (This logic is final and correct) ...
                    logger.info(f"Closed customer request task {taskk_id}. Initiating customer notifications.")
                    if customer_business_id:
                        customer_result = execute_query('SELECT "fcmToken", email, "customerName" FROM "customer" WHERE "customerId" = %s', [customer_business_id], fetch=True)
                        if customer_result:
                            customer_data = customer_result[0]
                            if customer_data.get('fcmToken'):
                                _send_notification(title="Request Completed", body=f"Your request '{task_name}' has been completed.", notification_type="TASK_COMPLETED", data_payload={"taskId": str(taskk_id), "type": "TASK_COMPLETED"}, fcm_token=customer_data.get('fcmToken'), delay_seconds=60, recipient_customer_id=customer_business_id)
                            if customer_data.get('email'):
                                customer_context = {
                                    "Customer Name": customer_data.get('customerName', 'Customer'),
                                    "Task Name": task_name,
                                    "Task id": taskk_id
                                }
                                send_mail_with_template(
                                    "Customer Request Completed", 
                                    customer_data['email'], 
                                    customer_context
                                )
                
                # --- PATH 2: It was an internal task closed by a Team Leader ---
                elif not was_requested:
                    user_is_team_leader = False
                    if request.user.roleId_id:
                        parent_role_query = """
                            SELECT parent_role."roleName" FROM userrole AS user_role
                            JOIN userrole AS parent_role ON user_role."reportingToRoleId" = parent_role."roleId"
                            WHERE user_role."roleId" = %s
                        """
                        parent_role_result = execute_query(parent_role_query, [request.user.roleId_id], fetch=True)
                        if parent_role_result and 'supervisor' in parent_role_result[0].get('roleName', '').lower():
                            user_is_team_leader = True
                    
                    if user_is_team_leader:
                        logger.info(f"Internal task {taskk_id} closed by Team Leader {request.user.userName}. Notifying Supervisor and Admin.")
                        
                        if not supervisor_employee_id:
                            logger.warning(f"Task {taskk_id}: Cannot notify because no supervisor is assigned.")
                        else:
                            supervisor_query = 'SELECT id, "fcmToken", "fullName", "reportingToId" FROM "user" WHERE "employeeId" = %s'
                            supervisor_result = execute_query(supervisor_query, [supervisor_employee_id], fetch=True)
                            
                            if supervisor_result:
                                supervisor_data = supervisor_result[0]
                                office_admin_user_id = supervisor_data.get('reportingToId')

                                # Action 1: Notify Supervisor (Push + Store)
                                _send_notification(
                                    title="Task Closed by Team Leader",
                                    body=f"The task '{task_name}' was closed by {request.user.fullName or request.user.userName}.",
                                    notification_type="TASK_CLOSED_BY_LEADER",
                                    data_payload={"taskId": str(taskk_id), "type": "TASK_CLOSED_BY_LEADER"},
                                    fcm_token=supervisor_data.get('fcmToken'),
                                    delay_seconds=60,
                                    recipient_user_id=supervisor_data['id']
                                )
                                
                                # Actions 2 & 3: Handle Office Admin
                                if office_admin_user_id:
                                    title = "Task Closed by Team Leader (FYI)"
                                    body = f"Task '{task_name}' assigned to '{supervisor_data.get('fullName', supervisor_employee_id)}' was closed by '{request.user.fullName or request.user.userName}'."
                                    
                                    # Action 2: Store Notification for Admin (No Push)
                                    _send_notification(
                                        title=title, body=body, notification_type="TASK_CLOSED_ADMIN_LOG",
                                        data_payload={"type": "TASK_CLOSED_ADMIN_LOG"},
                                        fcm_token=None,  # This ensures ONLY a DB record is created
                                        delay_seconds=60,
                                        recipient_user_id=office_admin_user_id
                                    )
                                    
                                    # Action 3: Send Email to Admin (using central email address)
                                    admin_email_result = execute_query('SELECT "officeAdminEmail" FROM "AdminSettings" WHERE COALESCE("isDeleted", 0) = 0 LIMIT 1', fetch=True)
                                    admin_email = admin_email_result[0]['officeAdminEmail'] if admin_email_result else None
                                    
                                    if admin_email:
                                        admin_context = {
                                            "Task Name": task_name,
                                            "Task id": taskk_id,
                                            "Supervisor Name": supervisor_data.get('fullName', supervisor_employee_id),
                                            "Team Leader Name": request.user.fullName or request.user.userName
                                        }
                                        send_mail_with_template(
                                            "Admin Task Closed Notification", 
                                            admin_email, 
                                            admin_context
                                        )
                                    else:
                                        logger.warning(f"Task {taskk_id}: Cannot send email to admin because no email is configured in AdminSettings.")
                            else:
                                logger.warning(f"Task {taskk_id}: Supervisor '{supervisor_employee_id}' not found.")
                    else:
                        logger.info(f"Task {taskk_id} closed by a non-Team Leader. No notifications sent.")

            # --- 6. Handle Response (Unchanged) ---
            return success_response(message=TASK_UPDATED_SUCCESSFULLY, status_code=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Critical error in TaskStatusUpdateView for task {taskk_id}: {e}", exc_info=True)
            return error_response(
                message=f"An error occurred while updating the task: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

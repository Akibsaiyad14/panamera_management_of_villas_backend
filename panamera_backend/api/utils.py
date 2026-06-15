import string
import secrets
import base64
from rest_framework import status
from rest_framework.response import Response
from django.db import connection
from django.utils import timezone
import datetime
from datetime import timedelta, date
from django.conf import settings
import json
import os
import firebase_admin
from firebase_admin import credentials, messaging, exceptions  
import threading
import time
import logging
from api.constants import CLEANUP_INTERVAL, LAST_LOG_CLEANUP_TIME
from django.db import transaction
from django.core.mail import send_mail
from psycopg2.extras import execute_values
from dateutil.relativedelta import relativedelta
from django.core.files.uploadedfile import UploadedFile
from api.constants import AMC_STATUS_COMPLETED
import shutil 
from dateutil.parser import parse
from api.constants import AMC_STATUS_ACTIVE, AMC_STATUS_INACTIVE
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
import traceback


logger = logging.getLogger(__name__)


def success_response(data = None, message = "", status_code=status.HTTP_200_OK, pagination=None):
    """
    Generates a standard success response.
    Accepts an optional pagination object to include in the response.
    """
    response_payload = {
        "status": True,
        "successMessage": message,
        "errorMessage": None,
        "data": data,
    }
    if pagination:
        response_payload["pagination"] = pagination
        
    return Response(response_payload, status=status_code)


def error_response(data=None, message="", status_code=status.HTTP_400_BAD_REQUEST):
    """
    Generates a standardized error response.
    """
    response_data = {
        "status": False,
        "successMessage": None,
        "errorMessage": message,
        "data": data if data is not None else None
    }
    return Response(response_data, status=status_code)



def generate_password(length=3):
    # First character: uppercase letter
    first_char = secrets.choice(string.ascii_uppercase)
    # Next two characters: lowercase letters
    next_two_chars = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(2))
    # Number part: digits, length based on 'length' parameter
    number_part = ''.join(secrets.choice(string.digits) for _ in range(length))
    # Combine all parts
    password = first_char + next_two_chars + number_part
    return password

def encode_password(password):
    encoded_bytes = base64.b64encode(password.encode("utf-8"))
    return encoded_bytes.decode("utf-8")

def decode_password(encoded_password):
    try:
        decoded = base64.b64decode(encoded_password).decode()
        return decoded
    except Exception:
        return None


def execute_query(query, params=None, fetch=True, many=False, commit=False):
    """
    Executes a SQL query using Django's database connection.

    Args:
        query (str): The SQL query to execute.
        params (tuple, optional): Parameters to pass to the query. Defaults to None.
        fetch (bool, optional): Whether to fetch results. Defaults to True.
        many (bool, optional): Whether to fetch multiple results (fetchall). Defaults to False.
        commit (bool, optional): Whether to commit the transaction. Defaults to False.

    Returns:
        list[dict] | bool: Returns list of results for SELECT queries,
                           True for successful UPDATE/INSERT/DELETE if fetch=True but no description (e.g. DML),
                           or if fetch=False.
                           False for failed operations (exceptions).
                           List of dicts if fetch=True and description exists.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            if commit: # This commit is for execute_query internal use.
                       # Better to manage transactions at view level with @transaction.atomic
                connection.commit()

            if fetch:
                if not cursor.description:
                    # For INSERT...RETURNING, cursor.description SHOULD be set by compliant drivers.
                    # If it's not, this function returns True.
                    # Also, if it's a DML without RETURNING and fetch=True was passed.
                    return True # Could be problematic if caller expected a list from RETURNING

                columns = [col[0] for col in cursor.description]
                if many:
                    results = cursor.fetchall()
                    return [dict(zip(columns, row)) for row in results] if results else []
                else:
                    row = cursor.fetchone()
                    return [dict(zip(columns, row))] if row else [] # Returns [] if no row
            else: # fetch=False (e.g., for UPDATE, DELETE, INSERT without RETURNING)
                  # cursor.rowcount could be checked here if needed, but True implies success
                return True # Assume success if no exception
    except Exception as e:
        print(f"Error executing query: {e}") # Good for debugging, consider formal logging
        # IMPORTANT: Rollback the transaction to prevent "current transaction is aborted" errors
        try:
            connection.rollback()
        except Exception as rollback_error:
            print(f"Error during rollback: {rollback_error}")
        return False


def add_new_skills(skill_set, execute_query_func):
    """
    Check if skills in skill_set are new, and insert them into SkillTest if they don't exist.

    Args:
        skill_set (str or list): Comma-separated string or list of skills
        execute_query_func (function): Function to execute raw SQL queries

    Returns:
        list: Clean list of skill names
    """
    if isinstance(skill_set, str):
        skill_set = [s.strip() for s in skill_set.split(",") if s.strip()]
    elif not isinstance(skill_set, list):
        raise ValueError("skillSet should be a comma-separated string or a list of skills")

    if not skill_set:
        raise ValueError("skillSet should not be empty")

    # Fetch existing skills
    existing_skills_query = 'SELECT "skillName" FROM "SkillTest"'
    existing_skills_result = execute_query_func(existing_skills_query, many=True)
    existing_skills = [skill["skillName"] for skill in existing_skills_result]

    # Identify new skills
    new_skills = [skill for skill in skill_set if skill not in existing_skills]

    # Insert new skills if any
    if new_skills:
        insert_skills_query = 'INSERT INTO "SkillTest" ("skillName") VALUES ' + ', '.join(["(%s)"] * len(new_skills))
        execute_query_func(insert_skills_query, new_skills)

    return skill_set



def get_aware_datetime(self, date_str):
    if not date_str:
        raise ValueError("dateTime is required.")
    return timezone.make_aware(datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S"), timezone.utc)

def make_aware(self, dt):
    return timezone.make_aware(dt, timezone.utc) if timezone.is_naive(dt) else dt

def get_today_attendance(self, user_id, today):
    result = execute_query("""
            SELECT id, "checkInTime", "checkOutTime", "assignedShiftAtCheckInId"
            FROM attendance
            WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
        """, [user_id, today])
    return result[0] if isinstance(result, list) and result else None

def calculate_overtime(self, shift_id, checkout_time, today):
    if not shift_id:
        return 0, 0

    shift_result = execute_query("""
            SELECT "endTime" FROM shifts WHERE "shiftId" = %s AND "isDeleted" = 0
        """, [shift_id])

    if not (isinstance(shift_result, list) and shift_result and shift_result[0].get("endTime")):
        return 0, 0

    shift_end = timezone.make_aware(datetime.combine(today, shift_result[0]["endTime"]), timezone.utc)
    if checkout_time <= shift_end:
        return 0, 0

    overtime_hours = round((checkout_time - shift_end).total_seconds() / 3600, 2)
    return overtime_hours, 1

def determine_attendance_status(self, hours, overtime_hours):
    if hours == 0:
        return 'ABSENT'
    elif hours <= 1.5:
        return 'HALFDAY'
    elif hours <= 5:
        return 'PRESENT'
    elif hours <= 6:
        return 'NORMAL'
    return 'OVERTIME' if overtime_hours > 0 else 'NORMAL'

def create_overtime_request(self, attendance_id, checkout_time):
    result = execute_query("""
            INSERT INTO "overtimeRequests" (
                "attendanceRecordId", "actualCheckoutTime",
                "createdAt", "updatedAt", "isDeleted"
            ) VALUES (%s, %s, %s, %s, 0)
            RETURNING "overtimeId"
        """, [attendance_id, checkout_time, timezone.now(), timezone.now()], fetch=True)

    if not (isinstance(result, list) and result and isinstance(result[0], dict)):
        return None
    return result[0].get("overtimeId")



def to_int_or_none(value):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None           

            # Helper function to parse and format date strings
def format_date(date_string):
    if not date_string:
        return None
    try:
        # dateutil.parser is very flexible and can handle '...T...' format
        return parse(date_string).strftime('%Y-%m-%d')
    except ValueError:
        return None

def detect_attachment_type(filename: str):
    """Return 0 for image, 1 for audio based on file extension."""
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".jfif"}
    audio_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".mp4"}
    ext = os.path.splitext(filename.lower())[1]
    if ext in image_exts:
        return 0
    elif ext in audio_exts:
        return 1
    return None  # unknown

def _calculate_attendance_details(checkin_time, checkout_time, shift_details, break_duration=timedelta(0)):
    """
    Calculates all attendance details dynamically based on shift data.
    - Assumes all datetimes are naive Dubai time.
    - Total work time = Check-out time - Check-in time
    - Overtime logic:
      * Overtime is based on total hours worked vs shift duration
      * Overtime only counts if total hours > shift duration + 30 minutes
      * Overtime hours = total hours - shift duration (when threshold is met)
    - Determines status (Halfday, Normal, Overtime, Absent) based on shift duration.
    - Returns a dictionary with total working hours, regular hours, overtime hours, and attendance status.
    """

    if not checkout_time:
        return {
            'total_working_hours': 0,
            'regular_hours': 0,
            'overtime_hours': 0,
            'attendance_status': 'CHECKED_IN'
        }

    # Total work time = Check-out time - Check-in time
    total_working_hours = round(max((checkout_time - checkin_time).total_seconds() / 3600, 0), 2)
    overtime_hours = 0
    attendance_date = checkin_time.date()

    if shift_details and shift_details.get('startTime') and shift_details.get('endTime'):
        # Build shift start and end naive datetimes
        shift_start_dt = datetime.combine(attendance_date, shift_details['startTime'])
        shift_end_dt = datetime.combine(attendance_date, shift_details['endTime'])

        # Handle overnight shifts
        if shift_end_dt <= shift_start_dt:
            shift_end_dt += timedelta(days=1)

        shift_duration_hours = (shift_end_dt - shift_start_dt).total_seconds() / 3600

        # NEW OVERTIME LOGIC:
        # Overtime is calculated based on total hours worked vs shift duration
        # Minimum threshold: 30 minutes beyond shift duration
        overtime_threshold_hours = shift_duration_hours + 0.5  # shift duration + 30 minutes
        
        if total_working_hours > overtime_threshold_hours:
            # Overtime = total hours worked - shift duration
            overtime_hours = round(max(0, total_working_hours - shift_duration_hours), 2)
        else:
            # No overtime if total hours <= shift duration + 30 minutes
            overtime_hours = 0

        regular_hours = total_working_hours - overtime_hours

        # Early checkout threshold: 30 minutes before shift end
        early_checkout_threshold_dt = shift_end_dt - timedelta(minutes=30)
        early_checkout_threshold_hours = (early_checkout_threshold_dt - checkin_time).total_seconds() / 3600

        # Determine attendance status based on percentage of shift worked
        if overtime_hours > 0:
            attendance_status = 'Overtime'
        elif total_working_hours == 0:
            attendance_status = 'Absent'
        elif total_working_hours < (shift_duration_hours / 2) or total_working_hours < early_checkout_threshold_hours:
            attendance_status = 'Halfday'
        elif total_working_hours <= shift_duration_hours:
            attendance_status = 'Normal'
        else:  # Worked 80%+ of shift
            attendance_status = 'Normal'

    else:
        # No shift assigned: fallback logic
        regular_hours = total_working_hours
        if total_working_hours == 0:
            attendance_status = 'Absent'
        elif total_working_hours < 3:
            attendance_status = 'Halfday'
        elif total_working_hours < 5:
            attendance_status = 'Normal'
        else:
            attendance_status = 'Normal'

    return {
        'total_working_hours': total_working_hours,
        'regular_hours': regular_hours,
        'overtime_hours': overtime_hours,
        'attendance_status': attendance_status,
        'shift_duration_hours': shift_duration_hours if shift_details else None
    }


SERVICE_ACCOUNT_KEY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'google-services.json')
# print(f"Using Firebase service account key at: {SERVICE_ACCOUNT_KEY_PATH}")

if not firebase_admin._apps:
    cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
    firebase_admin.initialize_app(cred)


def _send_push_notification_task(
    recipient_id,
    title,
    body,
    data_payload,
    fcm_token,
    delay_seconds=60,
    recipient_type='customer'
):
    """
    A task function that sends the push notification and handles FCM-specific errors.
    """
    try:
        # (This part of the code is correct and remains unchanged)
        print(f"THREAD: Staging push notification for {recipient_type} {recipient_id}. Will send in {delay_seconds} seconds.")
        time.sleep(delay_seconds)

        print(f"THREAD: Sending push notification NOW to {recipient_type} {recipient_id}...")
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data_payload,
            token=fcm_token,
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(aps=messaging.Aps(sound='default', badge=0))
            )
        )
        response = messaging.send(message)
        print(f"THREAD: Successfully sent push notification to {recipient_type} {recipient_id}. Response: {response}")

    # === THIS IS THE DEFINITIVE FIX ===
    # We catch each exception from its correct module.
    except (messaging.UnregisteredError, exceptions.NotFoundError) as e:
        # These errors mean the token is invalid or no longer registered with FCM.
        print(f"THREAD WARNING: FCM token for {recipient_type} {recipient_id} is invalid. Error: {str(e)}. Deleting token from database.")
        try:
            # Clean up the bad token so we don't try it again.
            if recipient_type == 'customer':
                table_name = '"customer"'
                id_column = '"customerId"'
            else: # Assuming the other type is 'user'
                table_name = '"user"'
                id_column = '"id"'

            cleanup_query = f'UPDATE {table_name} SET "fcmToken" = NULL WHERE {id_column} = %s'
            execute_query(cleanup_query, [recipient_id])
            print(f"THREAD: Successfully deleted invalid FCM token for {recipient_type} {recipient_id}.")
        except Exception as db_e:
            print(f"CRITICAL THREAD ERROR: Failed to delete invalid FCM token for {recipient_type} {recipient_id}. DB Error: {str(db_e)}")

    except Exception as e:
        # For all other unexpected errors
        print(f"CRITICAL THREAD ERROR sending push for {recipient_type} {recipient_id}. Unexpected Error: {str(e)}")
        traceback.print_exc()

def _send_notification(
    title,
    body,
    notification_type,
    data_payload,
    fcm_token=None,
    delay_seconds=60,
    attendance_id=None,
    recipient_user_id=None,      # <-- MODIFIED: Now optional
    recipient_customer_id=None,
    is_sync=False
):
    """
    Generic core function to store and send a notification to either a User or a Customer.
    """
    # Ensure that either a user ID or a customer ID is provided, but not both.
    if not (recipient_user_id or recipient_customer_id) or (recipient_user_id and recipient_customer_id):
        print("CRITICAL API ERROR: _send_notification called with invalid recipients.")
        return

    # Determine the recipient type and ID for logging/threading
    recipient_id = recipient_user_id or recipient_customer_id
    recipient_type = "user" if recipient_user_id else "customer"

    try:
        # === DYNAMIC INSERT QUERY BASED ON RECIPIENT TYPE ===
        if recipient_user_id:
            # We are notifying an internal user
            column_name = '"userId"'
            recipient_value = recipient_user_id
        else:
            # We are notifying an external customer
            column_name = '"customerId"'
            recipient_value = recipient_customer_id

        # Build the query and parameters dynamically
        base_query = f'INSERT INTO "notifications" ({column_name}, "title", "body", "type"'
        value_placeholders = '%s, %s, %s, %s'
        params = [recipient_value, title, body, notification_type]

        if attendance_id:
            base_query += ', "attendanceId"'
            value_placeholders += ', %s'
            params.append(attendance_id)

        insert_query = f'{base_query}) VALUES ({value_placeholders}) RETURNING "notificationId"'

        insert_result = execute_query(insert_query, params, fetch='one')

        # --- The rest of the function continues as before ---
        if not insert_result:
            print(f"ERROR: Failed to insert notification for {recipient_type} {recipient_id}.")
            return

        notification_id = insert_result[0]['notificationId']
        data_payload['notificationId'] = str(notification_id)

        execute_query("""
            UPDATE "notifications" SET "dataPayload" = %s::jsonb WHERE "notificationId" = %s
        """, [json.dumps(data_payload), notification_id])
        print(f"API: Stored and updated notification {notification_id} for {recipient_type} {recipient_id}.")

        if not fcm_token:
            print(f"API: {recipient_type.capitalize()} {recipient_id} has no FCM token. Notification stored but not sent.")
            return


        if is_sync:
            # Call the task function directly (no thread)
            _send_push_notification_task(
                recipient_id,
                title,
                body,
                data_payload,
                fcm_token,
                delay_seconds=0, # No delay for sync
                recipient_type=recipient_type
            )
        else:
            # Pass the generic recipient_id to the thread task
            thread = threading.Thread(
                target=_send_push_notification_task,
                args=(
                    recipient_id, # Pass the actual ID
                    title,
                    body,
                    data_payload,
                    fcm_token
                ),
                kwargs={
                    'delay_seconds': delay_seconds,
                    'recipient_type': recipient_type
                } # This is the correct way
            )
            thread.daemon = True
            thread.start()

        print(f"API: Queued push notification for {recipient_type} {recipient_id}. The API will now return a response.")

    except Exception as e:
        print(f"CRITICAL API ERROR processing notification for {recipient_type} {recipient_id}. Error: {str(e)}")


def send_overtime_notification(employee_id, employee_name, overtime_hours_decimal, attendance_id):
    """
    Prepares and sends an overtime notification to a manager. (No changes needed)
    """
    manager_query = execute_query("""
        SELECT manager.id, manager."fcmToken"
        FROM "user" employee JOIN "user" manager ON employee."reportingToId" = manager.id
        WHERE employee.id = %s
    """, [employee_id], many=False)
    if not manager_query: return
    manager_info = manager_query[0]

    overtime_formatted = f"{int(overtime_hours_decimal * 3600 // 3600):02d}:{int((overtime_hours_decimal * 3600 % 3600) // 60):02d}"
    data_payload = {'notificationType': 'overtime_request'}

    _send_notification(
        recipient_user_id=manager_info['id'],
        title="Overtime Alert",
        body=f"The employee {employee_name} has registered an overtime/early leave.",
        notification_type='EMPLOYEE_OVERTIME_ALERT',
        data_payload=data_payload,
        fcm_token=manager_info.get('fcmToken'),
        attendance_id=attendance_id  # Pass the attendance_id to the notification
    )


def send_early_leave_notification(employee_id, employee_name, attendance_id, early_reason):
    """
    Prepares and sends an early leave notification to a manager. (No changes needed)
    """
    manager_query = execute_query("""
        SELECT manager.id, manager."fcmToken"
        FROM "user" employee JOIN "user" manager ON employee."reportingToId" = manager.id
        WHERE employee.id = %s
    """, [employee_id], many=False)
    if not manager_query: return
    manager_info = manager_query[0]

    # truncated_reason = (early_reason[:100] + '...') if len(early_reason) > 100 else early_reason
    data_payload = {'notificationType': 'early_request'}

    _send_notification(
        recipient_user_id=manager_info['id'],
        title="Early Leave Alert",
        body=f"The employee {employee_name} has registered an overtime/early leave.",
        notification_type='EARLY_LEAVE_ALERT',
        data_payload=data_payload,
        fcm_token=manager_info.get('fcmToken'),
        attendance_id=attendance_id  # Pass the attendance_id to the notification
    )


def send_amc_completion_customer_push(customer_id, customer_name, amc_job_name, villa_name, visit_date):
    """Store and send a customer push notification when an AMC job is completed."""
    if not customer_id:
        return

    customer_result = execute_query(
        'SELECT "fcmToken" FROM "customer" WHERE "customerId" = %s AND COALESCE("isDeleted", 0) = 0 LIMIT 1',
        [customer_id],
        fetch='one'
    )

    customer_data = customer_result[0] if isinstance(customer_result, list) and customer_result else None
    customer_fcm_token = customer_data.get('fcmToken') if customer_data else None

    title = "AMC Job Completed"
    body = f"Hi {customer_name}, your AMC job '{amc_job_name}' at {villa_name} was completed on {visit_date}."
    data_payload = {
        "notificationType": "AMC_JOB_COMPLETED",
        "type": "AMC_JOB_COMPLETED",
        "customerId": str(customer_id),
    }

    _send_notification(
        recipient_customer_id=customer_id,
        title=title,
        body=body,
        notification_type="AMC_JOB_COMPLETED",
        data_payload=data_payload,
        fcm_token=customer_fcm_token,
        delay_seconds=0,
    )



def delete_old_overtime_folders(attendance_id, days=7):
    """Delete date-based subfolders under attendanceId older than 'days' days."""
    base_dir = os.path.join(settings.MEDIA_ROOT, 'overtime', str(attendance_id))
    if not os.path.exists(base_dir):
        return

    cutoff_date = datetime.now() - timedelta(days=days)

    for folder_name in os.listdir(base_dir):
        folder_path = os.path.join(base_dir, folder_name)
        if os.path.isdir(folder_path):
            try:
                folder_date = datetime.strptime(folder_name, '%Y-%m-%d')
                if folder_date < cutoff_date:
                    for root, dirs, files in os.walk(folder_path, topdown=False):
                        for file in files:
                            os.remove(os.path.join(root, file))
                        os.rmdir(root)
            except ValueError:
                continue  # Ignore folders not in date format


def save_overtime_files(attendance_id, files):
    """Save uploaded files to attendanceId/date folder with unique timestamped names."""
    saved_paths = []
    today_folder = datetime.now().strftime('%Y-%m-%d')
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'overtime', str(attendance_id), today_folder)
    os.makedirs(upload_dir, exist_ok=True)

    for file in files:
        filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.name}"
        file_path = os.path.join(upload_dir, filename)

        with open(file_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)

        relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT)
        final_path = os.path.join('media', relative_path).replace('\\', '/')
        saved_paths.append(final_path)

    return saved_paths


def save_emergency_files(attendance_id, files, file_type):
    """Save uploaded emergency check-in/out files (images or audio) to attendance-specific directory."""
    emergency_media_dir = os.path.join('emergency', str(attendance_id), file_type)
    full_media_path = os.path.join(settings.MEDIA_ROOT, emergency_media_dir)
    os.makedirs(full_media_path, exist_ok=True)

    saved_paths = []
    for f in files:
        filename = f.name
        
        # Normalize .jfif to .png to fix display issues
        if filename.lower().endswith('.jfif'):
            filename = filename[:-5] + '.png'
        
        file_path = os.path.join(full_media_path, filename)
        
        # Save as binary
        with open(file_path, 'wb') as destination:
            for chunk in f.chunks():
                destination.write(chunk)
        
        relative_path = os.path.join(emergency_media_dir, filename)
        saved_paths.append(relative_path.replace("\\", "/"))
    
    return saved_paths


def save_job_files(files, category, job_id):
    """
    Saves uploaded files to a structured directory and returns their relative paths.

    Args:
        files (list): A list of uploaded file objects from request.FILES.
        category (str): The top-level folder ('issues' or 'comments').
        job_id (int): The amcJobId to create a subfolder.

    Returns:
        list: A list of URL-friendly relative paths for the saved files.
    """
    saved_paths = []
    # Create the directory path, e.g., 'media/issues/12/' or 'media/comments/34/'
    upload_dir = os.path.join(settings.MEDIA_ROOT, category, str(job_id))
    os.makedirs(upload_dir, exist_ok=True)

    for file in files:
        # Create a unique filename to prevent overwrites
        filename = f"{file.name}"
        file_path = os.path.join(upload_dir, filename)

        # Write the file to the destination
        with open(file_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)

        # Get the relative path from MEDIA_ROOT to store in the database
        relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT)
        # Format the path to be a clean, URL-friendly string (e.g., 'issues/12/...')
        final_path = relative_path.replace('\\', '/')
        saved_paths.append(final_path)

    return saved_paths

def save_task_manager_files(files, task_id, file_type, delete_existing=False):
    """
    Saves uploaded files to a task-specific directory, optionally replacing existing files.
    e.g., /media/task_manager/101/images/photo.jpg
    """
    # The directory path is based on the task ID and type
    task_media_dir = os.path.join('task_manager', str(task_id), file_type)
    full_media_path = os.path.join(settings.MEDIA_ROOT, task_media_dir)

    # Delete existing files in the directory if specified (for PUT requests)
    if delete_existing and os.path.exists(full_media_path):
        shutil.rmtree(full_media_path)

    # Create the directory
    os.makedirs(full_media_path, exist_ok=True)

    saved_paths = []
    for f in files:
        filename = f.name

        # Normalize .jfif to .png to fix display issues (change to .jpg if you prefer)
        if filename.lower().endswith('.jfif'):
            filename = filename[:-5] + '.png'

        file_path = os.path.join(full_media_path, filename)

        # Save as binary
        with open(file_path, 'wb') as destination:
            for chunk in f.chunks():
                destination.write(chunk)

        relative_path = os.path.join(task_media_dir, filename)
        saved_paths.append(relative_path.replace("\\", "/"))

    return saved_paths

import smtplib
import re
from email.mime.text import MIMEText
import psycopg2
from django.conf import settings

def render_template_text(text, context):
    """Replace placeholders written as *Key* or plain Key with context values."""
    for key, value in context.items():
        placeholder = f"*{key}*"
        replacement = str(value)

        if placeholder in text:
            text = text.replace(placeholder, replacement)
            continue

        # Prevent accidental replacement of common standalone words like "Password"
        if str(key).lower() in ["password", "email", "username"]:
            continue

        # Also support templates saved without asterisks, e.g. "Customer Name".
        text = re.sub(rf"\b{re.escape(str(key))}\b", replacement, text)
    return text


def _build_email_message(subject, body, from_email, recipient_email):
    """Build a plain-text email message."""
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = recipient_email
    return msg

def send_mail_with_template(template_name, recipient_email, context):
    """
    Fetches an email template and SMTP settings from the database,
    populates the template with the given context, and sends the email.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            dbname=settings.DATABASES['default']['NAME'],
            user=settings.DATABASES['default']['USER'],
            password=settings.DATABASES['default']['PASSWORD'],
            host=settings.DATABASES['default']['HOST'],
            port=settings.DATABASES['default']['PORT']
        )
        with conn.cursor() as cursor:
            # 1. Get template from "mailTemplates" table
            cursor.execute("""
                SELECT subject, body 
                FROM "mailTemplates"
                WHERE "templateName" = %s AND COALESCE("isDeleted", 0) = 0
                LIMIT 1
            """, (template_name,))
            template = cursor.fetchone()
            if not template:
                print(f"ERROR: Email template '{template_name}' not found in the database.")
                raise Exception(f"Template '{template_name}' not found in DB")
            subject, body = template

            # 2. Replace placeholders in subject & body
            subject = render_template_text(subject, context)
            body = render_template_text(body, context)

            # Convert escaped formatting stored in DB (e.g. "\\n") to real characters.
            subject = subject.replace('\\n', ' ').replace('\\t', ' ')
            body = body.replace('\\n', '\n').replace('\\t', '\t')

            # 3. Get SMTP settings from "emailSettings" table
            cursor.execute("""
                SELECT "emailId", "appPassword", "smtpServer", "smtpPort"
                FROM "emailSettings"
                WHERE COALESCE("isDeleted", 0) = 0
                LIMIT 1
            """)
            smtp_settings = cursor.fetchone()
            if not smtp_settings:
                print("ERROR: No active SMTP settings found in the database.")
                raise Exception("No active SMTP settings found in DB")

            email_id, app_password, smtp_server, smtp_port = smtp_settings

        # 4. Send email using smtplib
        msg = _build_email_message(subject, body, email_id, recipient_email)

        print(f"Attempting to send email to {recipient_email} with subject '{subject}'")
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(email_id, app_password)
                server.sendmail(email_id, [recipient_email], msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(email_id, app_password)
                server.sendmail(email_id, [recipient_email], msg.as_string())
        print("Email sent successfully.")

    except Exception as e:
        print(f"Failed to send email using template '{template_name}'. Error: {str(e)}")
        # Depending on your needs, you might want to re-raise the exception
        # or just log it and continue. For now, we'll just print.
    finally:
        if conn:
            conn.close()


def send_mail_with_template_async(template_name, recipient_email, context):
    """Send template email in a background thread so API requests are not blocked by SMTP."""
    try:
        thread = threading.Thread(
            target=send_mail_with_template,
            args=(template_name, recipient_email, context),
            daemon=True,
        )
        thread.start()
    except Exception as e:
        print(
            f"Failed to start async email thread for template '{template_name}' to '{recipient_email}'. Error: {str(e)}"
        )


def send_plain_email_async(subject, message, recipient_email, from_email=None):
    """Send a plain text email in a background thread and swallow SMTP failures safely."""
    try:
        def _send():
            try:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=from_email or settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient_email],
                    fail_silently=True,
                )
            except Exception as email_error:
                logger.warning(
                    "Plain email send failed for %s: %s",
                    recipient_email,
                    email_error,
                    exc_info=True,
                )

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
    except Exception as e:
        logger.warning(
            "Failed to start plain email thread for %s. Error: %s",
            recipient_email,
            e,
            exc_info=True,
        )


def send_email_via_db_config(to_email, subject, body):
    """
    Sends a plain-text email using SMTP settings from the 'emailSettings' DB table.
    Supports both SSL (port 465) and STARTTLS (port 587) automatically.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            dbname=settings.DATABASES['default']['NAME'],
            user=settings.DATABASES['default']['USER'],
            password=settings.DATABASES['default']['PASSWORD'],
            host=settings.DATABASES['default']['HOST'],
            port=settings.DATABASES['default']['PORT']
        )
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT "emailId", "appPassword", "smtpServer", "smtpPort"
                FROM "emailSettings"
                WHERE COALESCE("isDeleted", 0) = 0
                LIMIT 1
            """)
            smtp_settings = cursor.fetchone()
            if not smtp_settings:
                raise Exception("No active SMTP settings found in DB")
            email_id, app_password, smtp_server, smtp_port = smtp_settings

        msg = _build_email_message(subject, body, email_id, to_email)

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(email_id, app_password)
                server.sendmail(email_id, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(email_id, app_password)
                server.sendmail(email_id, [to_email], msg.as_string())
    finally:
        if conn:
            conn.close()


def save_files_task_comments_issues(files, task_id=None, base_dir='task_attachments'):
    """
    Saves uploaded files (images or audios) to a task-specific directory locally.
    e.g., /media/task_attachments/101/image.jpg or /media/task_attachments/101/audio.mp3
    Returns a list of relative paths for jsonb storage.
    """
    # Base directory path
    if task_id:
        task_media_dir = os.path.join(base_dir, str(task_id))
    else:
        task_media_dir = os.path.join(base_dir, 'unattached')  # For cases without task_id
    full_media_path = os.path.join(settings.MEDIA_ROOT, task_media_dir)

    # Create the directory if it doesn't exist
    os.makedirs(full_media_path, exist_ok=True)

    saved_paths = []
    for f in files:
        filename = f.name
        # Normalize .jfif to .jpg to fix display issues
        if filename.lower().endswith('.jfif'):
            filename = filename[:-5] + '.jpg'
        
        # Ensure unique filename to avoid overwrites
        base_name, ext = os.path.splitext(filename)
        counter = 1
        unique_filename = filename
        while os.path.exists(os.path.join(full_media_path, unique_filename)):
            unique_filename = f"{base_name}_{counter}{ext}"
            counter += 1
        
        file_path = os.path.join(full_media_path, unique_filename)
        
        with open(file_path, 'wb') as destination:
            for chunk in f.chunks():
                destination.write(chunk)
        
        relative_path = os.path.join(task_media_dir, unique_filename).replace("\\", "/")
        saved_paths.append(relative_path)

    return saved_paths


def save_villa_images(file, villa_id):
    """
    Saves a single uploaded image to a villa-specific directory using its original filename.
    e.g., /media/villaImages/101/images/photo.jpg
    Returns the relative path or None if no valid file is provided.
    """
    if not file or not isinstance(file, UploadedFile):
        return None
    
    villa_media_dir = os.path.join('villaImages', str(villa_id), 'images')
    full_media_path = os.path.join(settings.MEDIA_ROOT, villa_media_dir)
    os.makedirs(full_media_path, exist_ok=True)

    filename = file.name
    file_path = os.path.join(full_media_path, filename)
    
    with open(file_path, 'wb+') as destination:
        for chunk in file.chunks():
            destination.write(chunk)
    
    relative_path = os.path.join(villa_media_dir, filename)
    return relative_path.replace("\\", "/")



def save_amc_document(job_id, file):
    """
    Saves an uploaded NOC document to the media directory within a job-specific folder.
    
    Args:
        job_id (str): The unique Job ID for creating the folder.
        file (UploadedFile): The file object from the request.

    Returns:
        str: The relative path to the saved file to be stored in the database,
             or None if no file is provided.
    """
    if not file:
        return None

    # Define the directory structure: media/amc_documents/<jobId>/
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'amc_documents', str(job_id))
    os.makedirs(upload_dir, exist_ok=True)

    # Create a unique filename using a timestamp to prevent overwrites
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.name.replace(' ', '_')}"
    file_path = os.path.join(upload_dir, filename)

    # Write the file content to the destination path
    with open(file_path, 'wb+') as destination:
        for chunk in file.chunks():
            destination.write(chunk)

    # Create a relative path for database storage and URL access (e.g., 'media/amc_documents/JOB001/...')
    relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT)
    # Ensure forward slashes for web compatibility
    final_path = os.path.join('media', relative_path).replace('\\', '/')
    
    return final_path



def delete_amc_document(document_path):
    """
    Deletes a document from the filesystem if it exists.
    
    Args:
        document_path (str): The path of the file relative to MEDIA_ROOT.
    """
    if not document_path:
        return # Nothing to delete

    # Construct the full absolute path to the file
    full_path = os.path.join(settings.MEDIA_ROOT, document_path)
    
    try:
        if os.path.exists(full_path):
            os.remove(full_path)
            logger.info(f"Successfully deleted old file: {full_path}")
        else:
            logger.warning(f"Attempted to delete a file that does not exist: {full_path}")
    except Exception as e:
        # Log any errors during deletion but don't crash the request
        logger.error(f"Error deleting file {full_path}: {e}")


from datetime import datetime
import pytz

def get_current_dubai_time():
    """
    This function is the ONLY reliable way to get the current time for this project.
    It calculates the current time in UTC and then converts it to 'Asia/Dubai' time.
    Finally, it strips the timezone information, returning a NAIVE datetime object
    that represents the correct local time in Dubai.

    This works regardless of the server's OS timezone.
    """
    # 1. Get the current time as a timezone-aware object in UTC. This is a reliable universal anchor.
    utc_now = datetime.now(pytz.utc)

    # 2. Define the Dubai timezone.
    dubai_tz = pytz.timezone('Asia/Dubai')

    # 3. Convert the UTC time to Dubai time. The result is still timezone-aware.
    dubai_now_aware = utc_now.astimezone(dubai_tz)

    # 4. Return a naive datetime object by removing the timezone info.
    # This is the final product that will be stored in the database.
    return dubai_now_aware.replace(tzinfo=None)


def _run_log_cleanup():
    """
    Deletes activity log entries older than 2 months from the activityLogs table.
    """
    try:
        # Calculate the timestamp for 2 months ago
        two_months_ago = datetime.now() - timedelta(days=60)

        # SQL query to delete records older than 2 months
        sql_query = """
            DELETE FROM "activityLogs"
            WHERE "logTimestamp" < %s;
        """
        params = (two_months_ago,)

        with connection.cursor() as cursor:
            cursor.execute(sql_query, params)
            deleted_count = cursor.rowcount
            logger.info("Cleaned up %s activity log entries older than 2 months.", deleted_count)

    except Exception as e:
        logger.error("Failed to clean up activity logs: %s", str(e))



def get_client_ip(request):
    """
    A reliable way to get the client's IP address, accounting for proxies.
    """
    x_forwarded_for = request.headers.get('x-forwarded-for')
    if x_forwarded_for:
        # The IP is the first one in the list if there are multiple
        ip = x_forwarded_for.split(',')[0]
    else:
        # Fallback to the standard remote address
        ip = request.META.get('REMOTE_ADDR')
    return ip

def format_deleted_employees(details):
    """Helper to format employee full names for delete action."""
    names = [d.get("fullName") for d in details.get("deletedAttendance", []) if d.get("fullName")]
    return ", ".join(names) if names else "N/A"

def get_leave_type_name(leave_type):
    """Helper to convert leave type number to name."""
    leave_types = {
        0: "Emergency",
        1: "Annual",
        2: "Sick"
    }
    return leave_types.get(leave_type, "Unknown")


def _generate_log_message(category, action, performer_name, target_employee_name, details, shift_name, performer_id=None, break_in_time=None, break_out_time=None):
    """
    Creates a human-readable log message based on the event type.
    """
    # Use a dictionary of templates for clean and maintainable message formats
    MESSAGE_TEMPLATES = {
        ('Authentication', 'Customer Login'): f"Customer login performed by {performer_name or 'N/A'}.",
        ('Authentication', 'Customer Logout'): f"Customer logout performed by {performer_name or 'N/A'}.",
        ('Authentication', 'Login'): f"Login performed by {performer_name or 'N/A'}.",
        ('Authentication', 'Logout'): f"Logout performed by {performer_name or 'N/A'}.",
        ('Attendance', 'CheckIn'): f"Check-in marked for employee {target_employee_name or 'N/A'}.",
        ('Attendance', 'CheckOut'): f"Check-out marked for employee {target_employee_name or 'N/A'}.",
        ('Attendance', 'OfflineCheckIn'): f"Offline check-in for user {performer_name} was synced. Checkin Time: {details.get('checkInTime', 'N/A')}.",
        ('Attendance', 'OfflineCheckInSyncOverride'): f"Automatic check-in for user {performer_name} was overridden by an offline sync. Manual Checkin Time: {details.get('checkInTime', 'N/A')}",
        ('Attendance', 'OfflineCheckOut'): f"Offline checkout for user {performer_name} was synced. Checkout Time: {details.get('checkOutTime', 'N/A')}.",
        ('Attendance', 'OfflineCheckOutSyncOverride'): f"Automatic checkout for user {performer_name} was overridden by an offline sync. Manual Checkout Time: {details.get('checkOutTime', 'N/A')}",
        ('Attendance', 'EmergencyCheckIn'): f"Emergency check-in marked for {target_employee_name or 'N/A'} at {details.get('emergencyCheckInTime', 'N/A')}.",
        ('Attendance', 'OfflineEmergencyCheckIn'): f"Offline emergency check-in for {target_employee_name or 'N/A'} synced. Time: {details.get('emergencyCheckInTime', 'N/A')}.",
        ('Attendance', 'EmergencyCheckOut'): f"Emergency check-out marked for {target_employee_name or 'N/A'} at {details.get('emergencyCheckOutTime', 'N/A')}. Emergency hours: {details.get('emergencyHours', 'N/A')}hrs.",
        ('Attendance', 'OfflineEmergencyCheckOut'): f"Offline emergency check-out for {target_employee_name or 'N/A'} synced. Time: {details.get('emergencyCheckOutTime', 'N/A')}, Emergency hours: {details.get('emergencyHours', 'N/A')}hrs.",
        ('Attendance', 'EmergencyMediaUpload'): f"Emergency media uploaded for {target_employee_name or 'N/A'}.",
        ('Attendance', 'OfflineEmergencyMediaUpload'): f"Offline emergency media synced for {target_employee_name or 'N/A'}.",
        ('Attendance', 'Add'): f" New Attendance record added for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}.",
        ('Attendance', 'Update'): f"Attendance record updated for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}.",
        ('Attendance', 'UpdateAttendanceStatus'): f"Attendance status updated to '{details.get('newStatus', 'N/A')}' for employee {details.get('userName', 'N/A')} | By: {performer_name or 'admin'}.",
        ('Attendance', 'BulkUpdateAttendanceStatus'): f"Attendance status updated to '{details.get('newStatus', 'N/A')}' for {details.get('count', 0)} employees | By: {performer_name or 'admin'}.",
        ('Attendance', 'Delete'): f"Attendance record(s) deleted for employee(s) {format_deleted_employees(details)} | By: {performer_name or 'admin'}.",
        ('Attendance', 'BreakIn'): f"Break-in time recorded | By: {performer_name or 'admin'}. Break-in time: {break_in_time}.",
        ('Attendance', 'BreakOut'): f"Break-out time recorded | By: {performer_name or 'admin'}. Break-out time: {break_out_time}.",
        ('Employee', 'Add'): f"New employee {target_employee_name or 'N/A'} added | By: {performer_name or 'admin'}.",
        ('Employee', 'Update'): f"Employee {target_employee_name or 'N/A'} updated | By: {performer_name or 'admin'}.",
        ('Employee', 'Delete'): f"Deleted {details.get('count', 0)} employee(s): [{', '.join(details.get('deletedEmployeeNames', [])) or 'N/A'}] | By: {performer_name}",
        ('Shift', 'Update'): f"Shift updated | By: {performer_name or 'admin'}. Shift Name: {shift_name}.",
        ('Shift', 'Delete'): f"Shift(s) deleted | By: {performer_name or 'admin'}.",
        ('ShiftMapping', 'Add'): f"Shift mapping added for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}.",
        ('ShiftMapping', 'Update'): f"Shift mapping updated for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}.",
        ('ShiftMapping', 'Delete'): f"Shift(s) mapping deleted for employee {target_employee_name or ''} | By: {performer_name or 'admin'}.",
        ('UserAccess', 'Add'): f"New user role '{details.get('newRoleName', 'N/A')}' created | By: {performer_name}",
        ('UserAccess', 'Update'): f"User role updated for Role Name: {details.get('roleName', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Export', 'Attendance'): f"Attendance data exported for date range {details.get('startDate')} to {details.get('endDate')} | By: {performer_name}",
        ('Export', 'Employee'): f"Employee data exported | By: {performer_name}",
        ('Export', 'AMCMaintenanceReport'): f"AMC Maintenance report export initiated for {details.get('startDate', 'N/A')} to {details.get('endDate', 'N/A')} | By: {performer_name}",
        ('Export', 'TaskIssueReport'): f"Task and Issue report export initiated for {details.get('startDate', 'N/A')} to {details.get('endDate', 'N/A')} | By: {performer_name}",
        ('Attendance', 'UpdateAttendanceStatus'): f"Attendance status updated to '{details.get('newStatus', 'N/A')}' for employee {details.get('userName', 'N/A')} | By: {performer_name or 'admin'}.",
        ('Attendance', 'BulkUpdateAttendanceStatus'): f"Attendance status updated to '{details.get('newStatus', 'N/A')}' for {details.get('count', 0)} employees | By: {performer_name or 'admin'}.",
        ('UserAccess', 'Delete'): f"{details.get('count', 0)} user roles deleted | By: {performer_name}",
        ('Overtime', 'Request'): f"Overtime requested for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}",
        ('Overtime', 'TLApproved'): f"Overtime TL-approved for employee {target_employee_name or 'N/A'} | TL Reason: {details.get('tlReasonText', 'N/A')} | Awaiting Supervisor approval | By: {performer_name or 'admin'}",
        ('Overtime', 'Approved'): f"Overtime final approved for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}",
        ('Overtime', 'Rejected'): f"Overtime rejected for employee {target_employee_name or 'N/A'} | By: {performer_name or 'admin'}",
        ('EarlyLeave', 'Request'): f"Early leave requested for employee {target_employee_name or 'N/A'} | By {performer_name or 'admin'}.",
        ('EarlyLeave', 'Approved'): f"Early leave approved for employee {target_employee_name or 'N/A'} | By {performer_name or 'admin'}.",
        ('EarlyLeave', 'Rejected'): f"Early leave rejected for employee {target_employee_name or 'N/A'} | By {performer_name or 'admin'}.",
        ('Shift', 'DeleteAll'): f"All shifts deleted | By {performer_name or 'admin'}.",
        ('Shift', 'Add'): f"New shift created | By {performer_name or 'admin'} Shift Name {shift_name}.",
        ('ShiftMapping', 'Assign'): f"Shift assigned to employee {target_employee_name or 'N/A'} | By {performer_name or 'admin'}.",
        ('ShiftMapping', 'Unassign'): f"Shift unassigned from employee {target_employee_name or 'N/A'} | By {performer_name or 'admin'}.",
        ('ShiftMapping', 'BulkAssign'): f"Shift assigned to {details.get('count', 0)} employees | By {performer_name or 'admin'}.",
        ('ShiftMapping', 'BulkUnassign'): f"Shift unassigned from {details.get('count', 0)} employees | By {performer_name or 'admin'}.",
        ('ShiftMapping', 'SetWeeklySchedule'): f"Weekly shift schedule set for employee {target_employee_name or details.get('userId', 'N/A')} with {details.get('daysAssigned', 0)} assigned day(s) | By: {performer_name or 'admin'}.",
        ('ShiftMapping', 'BulkSetWeeklySchedule'): f"Weekly shift schedule set for {details.get('count', 0)} employees | By: {performer_name or 'admin'}.",
        ('AMCJob', 'Add'): f"AMC Job Add - New AMC Job added with Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('AMCJob', 'Update'): f"AMC Job Update - AMC Job updated for Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('AMCJob', 'Delete'): f"AMC Job Delete - AMC Job deleted with Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('AMCJobCron', 'Execution'): f"AMC Job Cron Execution - Cron executed for JobID: {details.get('id', 'N/A')}, Title: {details.get('title', 'N/A')} | Next Schedule: {details.get('nextSchedule', 'N/A')} | By: System (Cron)",
        ('AMCJobCron', 'TaskGenerated'): f"AMC Job Cron Task Generated - New task(s) generated for JobID: {details.get('id', 'N/A')}, Title: {details.get('title', 'N/A')}, TaskID(s): {details.get('taskIds', 'N/A')} | By: System (Cron)",
        ('Task', 'Add'): f"Task Add - New task created Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Task', 'Update'): f"Task Update - Task updated Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Task', 'Delete'): f"Task Delete - Task deleted Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Task', 'StatusChange'): f"Task Status Change - Task status changed for Title: {details.get('title', 'N/A')} | Previous Status: {details.get('oldStatus', 'N/A')}, New Status: {details.get('newStatus', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Task', 'Assignment'): f"Task Assignment - Task assigned to Name: {details.get('employeeName', 'N/A')} | Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Issue', 'Add'): f"Issue Add - New issue created Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Issue', 'Update'): f"Issue Update - Issue updated Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Issue', 'Delete'): f"Issue Delete - Issue deleted Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Request', 'Add'): f"Request Add - New request created Title: {details.get('title', 'N/A')} | By: {details.get('customerName', 'N/A')}",
        ('Request', 'Update'): f"Request Update - Request updated Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Request', 'Delete'): f"Request Delete - Request deleted Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Request', 'Approve'): f"Request Approve - Request approved for Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Request', 'Reject'): f"Request Reject - Request rejected for Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('MailTemplate', 'Add'): f"Mail Template Add - New mail template added Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('MailTemplate', 'Update'): f"Mail Template Update - Mail template updated Title: {details.get('title', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Customer', 'Add'): f"Customer Add - New customer added Name: {details.get('name', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Customer', 'Update'): f"Customer Update - Customer updated Name: {details.get('name', 'N/A')} | By: {performer_name or details.get('name', 'N/A')}",
        ('Customer', 'Delete'): f"Customer Delete - Customer deleted Name: {details.get('name', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Customer', 'ResetPassword'): f"Customer Password reset for Name: {details.get('name', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Feedback', 'Add'): f"New feedback submitted by customer '{details.get('customerName', 'N/A')}'. Preview: \"{details.get('feedback_preview', '')}\"",
        # The delete template remains the same as it doesn't need the content.
        ('Feedback', 'Delete'): f"Feedback (ID: {details.get('feedbackId', 'N/A')}) from customer '{details.get('customerName', 'N/A')}' was deleted | By: {performer_name}",
        # ('TaskReminderCron', 'ExecutionStart'): "Task Reminder Cron - Execution started.",
        ('ReminderCron', 'NotificationSent'): f"Reminder Cron - Sent {details.get('item_type', 'Item')} reminder for '{details.get('item_name', 'N/A')}' to Supervisor: {details.get('supervisorName', 'N/A')}",

        ('ReminderCron', 'ExecutionSuccess'): f"Reminder Cron - Execution finished. Found {details.get('found_items', 0)} items, sent {details.get('sent_notifications', 0)} reminders.",

        ('ReminderCron', 'ExecutionError'): f"Reminder Cron - Execution failed. Error: {details.get('error', 'Unknown error')}.",
        ('AutoCheckoutCron', 'AutoCheckout'): f"System auto-checkout for employee {target_employee_name or 'N/A'}. Shift: {details.get('shiftName', 'N/A')}. Status: {details.get('attendanceStatus', 'N/A')}.",

        ('EmergencyRequest', 'Add'): f"Emergency request created for Customer ID: {details.get('customerId', 'N/A')} | Request ID: {details.get('requestId', 'N/A')} | Category: {details.get('category', 'N/A')} | Amount: {details.get('amount', 'N/A')} | By: {performer_name or 'N/A'}",
        ('EmergencyRequest', 'Assign'): f"Emergency request assigned for Request ID: {details.get('requestId', 'N/A')} | Team Leader: {details.get('teamLeaderName', details.get('teamLeaderId', 'N/A'))} | Supervisor: {details.get('supervisorName', details.get('supervisorId', 'N/A'))} | Assigned Time: {details.get('assignedTime', 'N/A')} | By: {performer_name or 'N/A'}",
        ('EmergencyRequest', 'Close'): f"Emergency request closed for Request ID: {details.get('requestId', 'N/A')} | Closed Time: {details.get('closedTime', 'N/A')} | By: {performer_name or 'N/A'}",
        ('EmergencyRequest', 'StatusUpdate'): f"Emergency request status updated for Request ID: {details.get('requestId', 'N/A')} | Status: {details.get('requestStatus', 'N/A')} | By: {performer_name or 'N/A'}",
        ('EmergencyRequest', 'PaymentUpdate'): f"Emergency request payment updated for Request ID: {details.get('requestId', 'N/A')} | Status: {details.get('paymentStatus', 'N/A')} | Transaction: {details.get('transactionId', 'N/A')} | By: {performer_name or 'N/A'}",
        ('EmergencyRequest', 'PaymentUpdateFailed'): f"Emergency request payment update failed for Request ID: {details.get('requestId', 'N/A')} | Status: {details.get('paymentStatus', 'N/A')} | Transaction: {details.get('transactionId', 'N/A')} | By: {performer_name or 'N/A'}",
        
        # Team Management
        ('Team', 'Create'): f"Team Create - New team '{details.get('teamName', 'N/A')}' created with leader {details.get('teamLeaderName', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Team', 'Update'): f"Team Update - Team '{details.get('teamName', 'N/A')}' updated | By: {performer_name or 'N/A'}",
        ('Team', 'Delete'): f"Team Delete - Team '{details.get('teamName', 'N/A')}' deleted | By: {performer_name or 'N/A'}",
        ('Team', 'AssignMembers'): f"Team Members Assigned - {details.get('memberCount', 0)} member(s) assigned to team '{details.get('teamName', 'N/A')}' | By: {performer_name or 'N/A'}",
        ('Team', 'RemoveMember'): f"Team Member Removed - {target_employee_name or 'N/A'} removed from team '{details.get('teamName', 'N/A')}' | By: {performer_name or 'N/A'}",
        
        # Leave Management
        ('Leave', 'Apply'): f"Leave Application - {performer_name or 'N/A'} applied for {get_leave_type_name(details.get('leaveType'))} leave (ID: {details.get('leaveId', 'N/A')}) from {details.get('startDate', 'N/A')} to {details.get('endDate', 'N/A')} ({details.get('totalDays', 0)} days)",
        ('Leave', 'Approve_TeamLeader'): f"Leave Approved by Team Leader - Leave application {details.get('leaveId', 'N/A')} approved by Team Leader | By: {performer_name or 'N/A'} | Stage: 1/3",
        ('Leave', 'Approve_Supervisor'): f"Leave Approved by Supervisor - Leave application {details.get('leaveId', 'N/A')} approved by Supervisor | By: {performer_name or 'N/A'} | Stage: 2/3",
        ('Leave', 'Approve_HR'): f"Leave Approved by HR - Leave application {details.get('leaveId', 'N/A')} approved by HR (Final Approval) | By: {performer_name or 'N/A'} | Stage: 3/3",
        ('Leave', 'Approve'): f"Leave Approved - Leave application {details.get('leaveId', 'N/A')} approved | By: {performer_name or 'N/A'}",
        ('Leave', 'Reject'): f"Leave Rejected - Leave application {details.get('leaveId', 'N/A')} rejected at approval stage {details.get('approvalStage', 'N/A')} | Reason: {details.get('rejectionReason', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Leave', 'AutoRejectCertificate'): f"Leave Auto-Rejected - Leave application of {target_employee_name or 'N/A'} for sick leave is rejected | Reason: {details.get('rejectionReason', 'N/A')} | By: {performer_name or 'System'}",
        ('Leave', 'Cancel'): f"Leave Cancelled - Leave application {details.get('leaveId', 'N/A')} cancelled | By: {performer_name or 'N/A'}",
        ('Leave', 'UploadCertificate'): f"Leave Certificate Uploaded - Certificate uploaded for leave application {details.get('leaveId', 'N/A')} | By: {performer_name or 'N/A'}",
        ('Leave', 'UpdateDates'): f"Leave Dates Updated - Leave application {details.get('leaveId', 'N/A')} dates updated | By: {performer_name or 'N/A'}",
        # AMC Job Comments
        ('AmcJobComment', 'ClearImages'): f"AmcJobComment - Images cleared for {details.get('visitType', 'N/A')} AMC Job Name: {details.get('amcJobName', 'N/A')} | By: {performer_name or 'N/A'}",

        # AMC Feedback
        ('AmcFeedback', 'Add'): f"AMC Feedback - Supervisor '{performer_name or 'N/A'}' submitted feedback  for AMC Name: {details.get('amcJobName', 'N/A')}",
        ('AmcFeedback', 'Update'): f"AMC Feedback - Feedback updated for AMC Name: {details.get('amcJobName', 'N/A')} | By: {performer_name or 'N/A'}",
        ('AmcFeedback', 'Delete'): f"AMC Feedback - Feedback for the AMC {details.get('amcJobName', 'N/A')} is deleted | By: {performer_name or 'N/A'}",
        ('AmcFeedback', 'CustomerAdd'): f"AMC Feedback - Customer (Name: {details.get('customerName', 'N/A')}) submitted feedback for the AMC : {details.get('amcJobName', 'N/A')}",

        # Material Request
        ('MaterialRequest', 'Add'): f"Material Request Add - New material request #{details.get('requestId', 'N/A')} created for Job: {details.get('jobNumber', 'N/A')} | Items: {details.get('itemCount', 0)} | By: {performer_name or 'N/A'}",
        ('MaterialRequest', 'Update'): f"Material Request Update - Request #{details.get('requestId', 'N/A')} updated | By: {performer_name or 'N/A'}",
        ('MaterialRequest', 'StatusUpdate'): f"Material Request Status Update - Request #{details.get('requestId', 'N/A')} status changed | By: {performer_name or 'N/A'}",
        ('MaterialRequest', 'SupervisorAssigned'): f"Material Request Supervisor Assigned - Supervisor assigned to request #{details.get('requestId', 'N/A')} | By: {performer_name or 'N/A'}",
        ('MaterialRequest', 'TeamLeaderAssigned'): f"Material Request Team Leader Assigned - Team leader assigned to request #{details.get('requestId', 'N/A')} | By: {performer_name or 'N/A'}",
        ('MaterialRequest', 'StatusUpdate, SupervisorAssigned'): f"Material Request Approved - Request #{details.get('requestId', 'N/A')} approved and supervisor assigned | By: {performer_name or 'N/A'}",
        ('MaterialRequest', 'Delete'): f"Material Request Delete - Request #{details.get('requestId', 'N/A')} deleted | By: {performer_name or 'N/A'}",

        # Stock Management list
        ('StockMaterial', 'Add'): f"Stock Add - New stock item '{details.get('stockCode', 'N/A')}' added | By: {performer_name or 'N/A'}",
        ('StockMaterial', 'Update'): f"Stock Update - Stock item '{details.get('stockCode', 'N/A')}' updated | By: {performer_name or 'N/A'}",
        ('StockMaterial', 'Delete'): f"Stock Delete - Stock item '{details.get('stockCodes', 'N/A')}' deleted | By: {performer_name or 'N/A'}",
    }

    # Get the specific template or use a generic fallback
    key = (category, action)
    template = MESSAGE_TEMPLATES.get(key)

    if template:
        return template
    else:
        # Generic fallback message for any actions not defined above
        return f"Action '{action}' in category '{category}' performed by {performer_name or 'system'} on employee {target_employee_name or 'N/A'}."


def _get_display_name(person):
    if not person:
        return None

    if isinstance(person, dict):
        return (
            person.get('fullName')
            or person.get('customerName')
            or person.get('userName')
            or person.get('name')
            or person.get('username')
        )

    return (
        getattr(person, 'fullName', None)
        or getattr(person, 'customerName', None)
        or getattr(person, 'userName', None)
        or getattr(person, 'name', None)
        or getattr(person, 'username', None)
        or str(person)
    )

class CustomJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder to handle non-serializable types like datetime.
    """
    def default(self, obj):
        # If the object is a datetime or date object, convert it to a string
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()  # isoformat() is a standard string representation

        # Let the base class default method raise the TypeError for other types
        return super().default(obj)


# --- THE MAIN LOGGING FUNCTION (ENHANCED) ---
def log_activity_raw(
    request,
    category,
    action,
    performer=None,
    target_employee_name=None,
    target_shift_id=None,
    details=None,
    shift_name = None,
    break_in_time=None,
    break_out_time=None
):
    """
    Creates a full activity log, extracting all device info from headers.
    """

    _run_log_cleanup()


    try:
        log_timestamp = datetime.now()

        # --- 1. GET PERFORMER DETAILS ---
        performer_id = str(getattr(performer, 'id', None)) if performer and getattr(performer, 'id', None) is not None else None
        performer_name = _get_display_name(performer)

        # --- 2. GET ALL DEVICE & NETWORK INFO FROM HEADERS ---
        # Note: We use .get() which safely returns None if the header is missing.
        if request:
            device = request.headers.get('Device-Type', 'Dashboard')
            app_version = request.headers.get('App-Version', '1.0.0')
            mac_id = request.headers.get('Mac-Id', 'N/A')
            ip_address = get_client_ip(request) # Use the helper for reliability
        else:
            device = 'Server Cron'
            app_version = 'N/A'
            mac_id = 'N/A'
            ip_address = 'localhost'

        # --- 3. PREPARE OTHER DATA ---
        details_data = details or {}
        # details_json = json.dumps(details_data)
        details_json = json.dumps(details_data, cls=CustomJSONEncoder)
        log_message = _generate_log_message(category, action, performer_name, target_employee_name, details_data, shift_name, performer_id, break_in_time, break_out_time)

        # --- 4. CONSTRUCT FINAL QUERY & PARAMS ---
        sql_query = """
            INSERT INTO "activityLogs" (
                "logTimestamp", "eventCategory", "eventAction", "userId",
                "userName", "employeeId", "shiftId", "device",
                "macId", "appVersion", "details", "logMessage", "ipAddress"
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        params = (
            log_timestamp, category, action, performer_id, performer_name,
            str(target_employee_name) if target_employee_name else None,
            str(target_shift_id) if target_shift_id else None,
            device, mac_id, app_version, details_json, log_message, ip_address
        )

        with connection.cursor() as cursor:
            cursor.execute(sql_query, params)

    except Exception as e:
        logger.error("Failed to create raw activity log: %s", str(e))



def create_or_update_amc_schedules(amc_id, scopes_of_work, garden_visit_days, pool_visit_days):
    """
    Updates or creates AMC schedules based on provided scopes and visit days for both garden and pool.
    
    Args:
        amc_id: The AMC master ID
        scopes_of_work: List of work types (e.g., ['Garden Maintenance', 'Pool Maintenance'])
        garden_visit_days: List of days for garden visits (e.g., ['Monday', 'Wednesday'])
        pool_visit_days: List of days for pool visits (e.g., ['Tuesday', 'Thursday'])
    """
    if not isinstance(scopes_of_work, list):
        return 
    if not isinstance(garden_visit_days, list):
        garden_visit_days = []
    if not isinstance(pool_visit_days, list):
        pool_visit_days = []

    with transaction.atomic():
        # 1. Get existing AMCSchedule rows to determine which to delete
        fetch_schedules_query = """
            SELECT "scheduleId", "maintenanceId", "assignedDay"
            FROM "AMCSchedule"
            WHERE "amcId" = %s
        """
        existing_schedules = execute_query(fetch_schedules_query, [amc_id], many=True)
        print(f"Found {len(existing_schedules)} existing schedules for AMC {amc_id}.")
        
        existing_schedule_keys = {(s['maintenanceId'], s['assignedDay']) for s in existing_schedules}
        
        if not scopes_of_work or (not garden_visit_days and not pool_visit_days):
            # If no scopes or days, delete all existing schedules
            schedules_to_delete = [s['scheduleId'] for s in existing_schedules]
        else:
            # 2. Get task templates for both garden and pool
            get_templates_query = """
                SELECT "maintenanceId", "type", "category" 
                FROM "amcMaintenanceTasks" 
                WHERE "type" = ANY(%s)
            """
            templates = execute_query(get_templates_query, [scopes_of_work], many=True)
            print(f"Found {len(templates)} task templates for AMC {amc_id} with scopes {scopes_of_work}.")
            
            if not templates:
                schedules_to_delete = [s['scheduleId'] for s in existing_schedules]
            else:
                # Separate templates by type field (Garden Maintenance / Pool Maintenance)
                garden_templates = [t for t in templates if 'garden' in t.get('type', '').lower()]
                pool_templates = [t for t in templates if 'pool' in t.get('type', '').lower()]
                
                # Build new schedule keys
                new_schedule_keys = set()
                
                # Add garden schedules
                for template in garden_templates:
                    for day in garden_visit_days:
                        new_schedule_keys.add((template['maintenanceId'], day))
                
                # Add pool schedules
                for template in pool_templates:
                    for day in pool_visit_days:
                        new_schedule_keys.add((template['maintenanceId'], day))
                
                # 4. Determine schedules to delete: those not in the new desired set
                schedules_to_delete = [
                    s['scheduleId'] for s in existing_schedules
                    if (s['maintenanceId'], s['assignedDay']) not in new_schedule_keys
                ]
        
        # 5. Delete affected JobTasks rows (if they exist)
        if schedules_to_delete:
            delete_job_tasks_query = """
                DELETE FROM "JobTasks"
                WHERE "scheduleId" = ANY(%s)
            """
            execute_query(delete_job_tasks_query, [schedules_to_delete])

        # 6. Delete unneeded AMCSchedule rows
        if schedules_to_delete:
            delete_query = 'DELETE FROM "AMCSchedule" WHERE "scheduleId" = ANY(%s)'
            execute_query(delete_query, [schedules_to_delete])

        # Early return if no new scopes/days/templates
        if not scopes_of_work or (not garden_visit_days and not pool_visit_days) or not templates:
            return

        # 7. Insert new schedules
        schedules_to_insert = []
        
        # Insert garden schedules
        for template in garden_templates:
            for day in garden_visit_days:
                if (template['maintenanceId'], day) not in existing_schedule_keys:
                    schedules_to_insert.append((amc_id, template['maintenanceId'], day))
        
        # Insert pool schedules
        for template in pool_templates:
            for day in pool_visit_days:
                if (template['maintenanceId'], day) not in existing_schedule_keys:
                    schedules_to_insert.append((amc_id, template['maintenanceId'], day))

        if schedules_to_insert:
            insert_query = """
                INSERT INTO "AMCSchedule" ("amcId", "maintenanceId", "assignedDay")
                VALUES (%s, %s, %s)
                ON CONFLICT ("amcId", "maintenanceId", "assignedDay") DO NOTHING;
            """
            for schedule_data in schedules_to_insert:
                execute_query(insert_query, schedule_data)


def generate_jobs_and_tasks_for_amc(amc_id, start_date, end_date):
    """
    Generates AmcJobs (visits) and their associated lightweight VisitTasks for a
    SINGLE AMC within a given date range. Creates SEPARATE jobs for garden and pool.
    
    This function creates jobs with proper visitType:
    - visitType=1: Garden only (created on garden visit days)
    - visitType=2: Pool only (created on pool visit days)
    
    When both garden and pool fall on the same day, TWO separate jobs are created.

    Returns:
        (int, int): A tuple containing (number_of_jobs_processed, number_of_tasks_created)
    """
    # 1. Get the master contract details, including BOTH garden and pool visit days
    query = """
        SELECT
            m."startDate",
            m.duration,
            m."gardenVisitDays",
            m."poolVisitDays",
            m."amcJobName",
            m."villaId",
            c."customerName",
            COALESCE(su."fullName", su.name, su."userName") AS "supervisorName",
            
            -- Aggregate maintenance task IDs with their types
            jsonb_agg(
                jsonb_build_object(
                    'maintenanceId', s."maintenanceId",
                    'type', mt.type
                )
            ) AS maintenance_tasks
        FROM "AMCMaster" m
        JOIN "AMCSchedule" s ON m."amcId" = s."amcId"
        JOIN "amcMaintenanceTasks" mt ON s."maintenanceId" = mt."maintenanceId"
        LEFT JOIN "customer" c ON m."customerId" = c."customerId" AND COALESCE(c."isDeleted", 0) = 0
        LEFT JOIN "user" su ON m."gardenSupervisorId" = su."employeeId" AND COALESCE(su."isDeleted", 0) = 0
        WHERE m."amcId" = %s AND m."isDeleted" = 0 AND m.status = %s AND s."isActive" = true
        GROUP BY 
            m."amcId",
            m."jobId",
            m."amcJobName",
            m."startDate",
            m.duration,
            m."gardenVisitDays",
            m."poolVisitDays",
            m."villaId",
            c."customerName",
            su."fullName",
            su.name,
            su."userName"
        ORDER BY m."startDate" DESC;
    """
    params = [amc_id, AMC_STATUS_ACTIVE]
    contract_data = execute_query(query, params, fetch='one')

    # Handle case where execute_query returns a list
    if isinstance(contract_data, list) and contract_data:
        contract_data = contract_data[0]
    
    # If no contract is found or it has no scheduled tasks, there's nothing to do.
    if not contract_data or not contract_data.get('maintenance_tasks'):
        return 0, 0

    # 2. Process visit days for both garden and pool
    day_map = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6}
    
    # Parse garden visit days
    garden_days_json = contract_data.get('gardenVisitDays', [])
    if isinstance(garden_days_json, str):
        try: garden_days_json = json.loads(garden_days_json)
        except json.JSONDecodeError: garden_days_json = []
    garden_weekdays = {day_map[day.lower()] for day in garden_days_json if day.lower() in day_map}
    
    # Parse pool visit days
    pool_days_json = contract_data.get('poolVisitDays', [])
    if isinstance(pool_days_json, str):
        try: pool_days_json = json.loads(pool_days_json)
        except json.JSONDecodeError: pool_days_json = []
    pool_weekdays = {day_map[day.lower()] for day in pool_days_json if day.lower() in day_map}
    
    # Separate maintenance tasks by type field (Garden Maintenance / Pool Maintenance)
    maintenance_tasks = contract_data.get('maintenance_tasks', [])
    if isinstance(maintenance_tasks, str):
        try: maintenance_tasks = json.loads(maintenance_tasks)
        except json.JSONDecodeError: maintenance_tasks = []
    
    garden_task_ids = [t['maintenanceId'] for t in maintenance_tasks if 'garden' in t.get('type', '').lower()]
    pool_task_ids = [t['maintenanceId'] for t in maintenance_tasks if 'pool' in t.get('type', '').lower()]
    
    contract_start_date_obj = contract_data.get('startDate')
    duration_months = contract_data.get('duration')

    if not contract_start_date_obj or duration_months is None:
        return 0, 0

    contract_start_date = contract_start_date_obj.date()
    contract_end_date = contract_start_date + relativedelta(months=duration_months)

    # 3. Calculate visit dates - create SEPARATE entries for garden and pool
    # Each entry is a tuple: (date, visitType, task_ids)
    visit_jobs = []
    current_date = start_date
    
    while current_date <= end_date:
        if contract_start_date <= current_date < contract_end_date:
            weekday = current_date.weekday()
            is_garden_day = weekday in garden_weekdays
            is_pool_day = weekday in pool_weekdays
            
            # Create separate job for garden if it's a garden day
            if is_garden_day and garden_task_ids:
                visit_jobs.append((current_date, 1, garden_task_ids))  # visitType=1 for Garden
            
            # Create separate job for pool if it's a pool day
            if is_pool_day and pool_task_ids:
                visit_jobs.append((current_date, 2, pool_task_ids))  # visitType=2 for Pool
        
        current_date += timedelta(days=1)

    if not visit_jobs:
        return 0, 0

    jobs_processed = 0
    tasks_created = 0

    # 4. Use a single transaction for all database operations
    with transaction.atomic():
        with connection.cursor() as cursor:
            for visit_date, visit_type, task_ids_for_this_visit in visit_jobs:
                jobs_processed += 1
                
                # Step A: Check if job already exists for this amcId, visitDate, visitType
                cursor.execute(
                    'SELECT "amcJobId" FROM "AmcJobs" WHERE "amcId" = %s AND "visitDate" = %s AND "visitType" = %s',
                    [amc_id, visit_date, visit_type]
                )
                existing_job = cursor.fetchone()
                
                if existing_job:
                    # Update existing job
                    amc_job_id = existing_job[0]
                    update_job_query = """
                        UPDATE "AmcJobs" 
                        SET "amcJobName" = %s, "customerName" = %s, "supervisorName" = %s, "villaId" = %s
                        WHERE "amcJobId" = %s
                    """
                    cursor.execute(update_job_query, [
                        contract_data['amcJobName'],
                        contract_data.get('customerName'),
                        contract_data.get('supervisorName'),
                        contract_data.get('villaId'),
                        amc_job_id
                    ])
                else:
                    # Insert new job
                    insert_job_query = """
                        INSERT INTO "AmcJobs" 
                        ("amcId", "visitDate", "amcJobName", "customerName", "supervisorName", "villaId", "visitType")
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING "amcJobId";
                    """
                    cursor.execute(insert_job_query, [
                        amc_id,
                        visit_date,
                        contract_data['amcJobName'],
                        contract_data.get('customerName'),
                        contract_data.get('supervisorName'),
                        contract_data.get('villaId'),
                        visit_type
                    ])
                    result = cursor.fetchone()
                    amc_job_id = result[0]

                # Step B: Insert tasks for this job - use ON CONFLICT to handle duplicates safely
                if task_ids_for_this_visit:
                    # Insert each task individually with ON CONFLICT to prevent duplicates
                    for m_id in task_ids_for_this_visit:
                        cursor.execute(
                            """
                            INSERT INTO "VisitTasks" ("amcJobId", "maintenanceId")
                            VALUES (%s, %s)
                            ON CONFLICT ("amcJobId", "maintenanceId") DO NOTHING
                            """,
                            [amc_job_id, m_id]
                        )
                        # Check if a row was actually inserted
                        if cursor.rowcount > 0:
                            tasks_created += 1

    return jobs_processed, tasks_created



def save_project_images(files, base_dir='project_images'):
    """
    Saves uploaded project images into /media/project_images/
    Returns list of relative paths for DB storage.
    """
    upload_dir = os.path.join(settings.MEDIA_ROOT, base_dir)
    os.makedirs(upload_dir, exist_ok=True)

    saved_paths = []
    for f in files:
        filename = f.name

        # Ensure unique filename
        base_name, ext = os.path.splitext(filename)
        counter = 1
        unique_filename = filename
        while os.path.exists(os.path.join(upload_dir, unique_filename)):
            unique_filename = f"{base_name}_{counter}{ext}"
            counter += 1

        file_path = os.path.join(upload_dir, unique_filename)
        with open(file_path, 'wb') as destination:
            for chunk in f.chunks():
                destination.write(chunk)

        relative_path = os.path.join(base_dir, unique_filename).replace("\\", "/")
        saved_paths.append(relative_path)

    return saved_paths



def _send_email(email_config, recipient_email, subject, body):
        """Helper function to send an email using SMTP settings from the database."""
        if not email_config:
            print("ERROR: Cannot send email because email_config is not loaded.")
            return False
        if not recipient_email:
            print(f"ERROR: Cannot send email because recipient_email is not provided for subject: {subject}")
            return False
            
        try:
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['From'] = email_config['emailId']
            msg['To'] = recipient_email
            msg['Subject'] = subject

            server = smtplib.SMTP(email_config['smtpServer'], email_config['smtpPort'])
            server.starttls()
            server.login(email_config['emailId'], email_config['appPassword'])
            text = msg.as_string()
            server.sendmail(email_config['emailId'], recipient_email, text)
            server.quit()
            print(f"Successfully sent email to {recipient_email}")
            return True
        except Exception as e:
            print(f"FAILED to send email to {recipient_email}: {e}")
            return False


def to_relative_media_path(path):
    if not path:
        return None

    # Normalize slashes
    normalized = path.replace("\\", "/")
    media_root = settings.MEDIA_ROOT.replace("\\", "/")

    # ✅ Remove MEDIA_ROOT prefix from DB stored path
    if normalized.startswith(media_root):
        relative = normalized[len(media_root):].lstrip("/")
        return relative

    # ✅ Remove "/media/" prefix if appears
    if "/media/" in normalized:
        return normalized.split("/media/", 1)[1]

    return normalized


def format_hours_to_hhmm(hours_val):
            """Converts float hours (e.g., 1.5) to string '01:30 hrs'"""
            if not hours_val:
                return "00:00 hrs"
                
            # Convert total hours to total minutes (rounding to nearest minute)
            total_minutes = int(round(hours_val * 60))
            
            # Calculate hours and remaining minutes
            hours = total_minutes // 60
            minutes = total_minutes % 60
            
            # Return formatted string with leading zeros
            return f"{hours:02d}:{minutes:02d} hours"



def save_leave_certificate(file, leave_id):
    """
    Saves uploaded leave certificate to a leave-specific directory.
    e.g., /media/leave_certificates/101/certificate.pdf
    Returns the relative path or None on error.
    """
    try:
        leave_media_dir = os.path.join('leave_certificates', str(leave_id))
        full_media_path = os.path.join(settings.MEDIA_ROOT, leave_media_dir)
        
        # Create the directory
        os.makedirs(full_media_path, exist_ok=True)
        
        filename = file.name
        
        # Generate unique filename to avoid overwrites
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name, ext = os.path.splitext(filename)
        unique_filename = f"{name}_{timestamp}{ext}"
        
        file_path = os.path.join(full_media_path, unique_filename)
        
        # Save as binary
        with open(file_path, 'wb') as destination:
            for chunk in file.chunks():
                destination.write(chunk)
        
        relative_path = os.path.join(leave_media_dir, unique_filename)
        return relative_path.replace("\\", "/")
    
    except Exception as e:
        print(f"ERROR: Failed to save leave certificate: {str(e)}")
        return None

def format_datetime_value(datetime_string):
    if not datetime_string:
        return None
    try:
        parsed_datetime = parse(datetime_string)
        if timezone.is_naive(parsed_datetime):
            parsed_datetime = timezone.make_aware(parsed_datetime, timezone.get_current_timezone())
            # print(parsed_datetime)
        return parsed_datetime
    except (ValueError, TypeError, OverflowError):
        return None

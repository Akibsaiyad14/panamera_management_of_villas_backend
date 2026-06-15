# your_app/management/commands/test_notification.py

import os
import json
import firebase_admin
from firebase_admin import credentials, messaging
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from api.utils import execute_query  # Make sure to adjust the import path to your project structure

# Adjust the import path to your execute_query helper
# from your_project.utils import execute_query 

class Command(BaseCommand):
    help = 'Sends a test push notification to a specified user by their ID or email.'

    def add_arguments(self, parser):
        """Adds command-line arguments."""
        parser.add_argument('user_identifier', type=str, help='The ID or email of the user to send the notification to.')
        parser.add_argument(
            '--store',
            action='store_true',  # This makes it a flag, e.g., --store
            help='Also store the notification in the database.'
        )

    def handle(self, *args, **options):
        """The main logic of the command."""
        user_identifier = options['user_identifier']
        should_store = options['store']

        # --- Initialize Firebase Admin SDK ---
        # This is necessary because the command is a separate process.
        if not firebase_admin._apps:
            service_account_path = os.path.join(settings.BASE_DIR, 'firebase-service-account.json')
            if not os.path.exists(service_account_path):
                self.stderr.write(self.style.ERROR(f"Firebase service account key not found at: {service_account_path}"))
                return
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            self.stdout.write("Firebase Admin SDK initialized.")

        # --- Find the user and their FCM token ---
        is_id = False
        try:
            # Check if the identifier is a numeric ID
            user_id = int(user_identifier)
            query = 'SELECT id, "fcmToken", "fullName" FROM "user" WHERE id = %s'
            params = [user_id]
            is_id = True
        except ValueError:
            # Assume it's an email
            query = 'SELECT id, "fcmToken", "fullName" FROM "user" WHERE email = %s'
            params = [user_identifier]
        
        self.stdout.write(f"Searching for user with {'ID' if is_id else 'email'}: {user_identifier}")
        user_data = execute_query(query, params, many=False)

        if not user_data:
            self.stderr.write(self.style.ERROR(f"User not found: {user_identifier}"))
            return
        
        user_info = user_data[0]
        user_db_id = user_info['id']
        user_name = user_info.get('fullName', 'User')
        user_token = user_info.get('fcmToken')

        if not user_token:
            self.stderr.write(self.style.ERROR(f"User '{user_name}' (ID: {user_db_id}) found, but they have no FCM token in the database."))
            self.stderr.write("The user needs to log in to the mobile app at least once to register their device.")
            return
        
        self.stdout.write(self.style.SUCCESS(f"Found user '{user_name}' with FCM token: {user_token[:20]}..."))

        # --- Prepare Notification Content ---
        title = "Test Notification"
        body = f"Hi {user_name}, this is a test notification sent at {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}."
        data_payload = {
        'notificationType': 'early_request',
        'notificationId': '2',  # Example ID, can be anything for testing
        
    }

        # --- Optionally store in DB ---
        if should_store:
            self.stdout.write("Storing notification in the database...")
            try:
                execute_query("""
                    INSERT INTO "notifications" ("userId", "title", "body", "type", "dataPayload")
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                """, [user_db_id, title, body, 'TEST_ALERT', json.dumps(data_payload)])
                self.stdout.write(self.style.SUCCESS("Successfully stored notification in the database."))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Failed to store notification in DB: {e}"))
                return


        # --- Construct and Send the Message ---
        self.stdout.write("Constructing FCM message...")
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data_payload,
            token=user_token,
            apns=messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(badge=0, sound='default')))
        )

        try:
            self.stdout.write("Sending push notification via FCM...")
            response = messaging.send(message)
            self.stdout.write(self.style.SUCCESS(f"Successfully sent notification! FCM Response: {response}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to send FCM notification. Error: {e}"))
            self.stderr.write("This often happens if the FCM token is old or invalid. Ask the user to log out and log back in.")


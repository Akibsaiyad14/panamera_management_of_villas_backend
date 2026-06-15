import smtplib
import socket
from email.mime.text import MIMEText
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response


class EmailSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Fetch the active email settings record (only one expected)
        """
        try:
            query = """
                SELECT id, "emailId", "appPassword", "smtpServer", "smtpPort"
                FROM "emailSettings"
                WHERE "isDeleted" = 0
                ORDER BY id DESC
                LIMIT 1
            """
            result = execute_query(query, fetch="one")

            # Your execute_query returns list of dicts
            if isinstance(result, list):
                if len(result) == 0:
                    return error_response(message="No active email settings found", status_code=status.HTTP_404_NOT_FOUND)
                result = result[0]

            if not result:
                return error_response(message="No active email settings found", status_code=status.HTTP_404_NOT_FOUND)

            result["appPassword"] = "********"  # Mask the password in the response

            # result is already a dict
            email_setting = {
                "id": result["id"],
                "emailId": result["emailId"],
                "appPassword": result["appPassword"],
                "smtpServer": result["smtpServer"],
                "smtpPort": result["smtpPort"],
            }

            return success_response(data=email_setting, message="Email setting fetched successfully", status_code=status.HTTP_200_OK)

        except Exception as e:
            return error_response(message=f"Error fetching email setting: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def put(self, request):
        """
        Validate email by sending a test email, then update DB if valid
        """
        try:
            data = request.data
            required_fields = ["emailId", "appPassword", "smtpServer", "smtpPort"]
            missing = [f for f in required_fields if not data.get(f)]
            if missing:
                return error_response(message=f"Missing fields: {', '.join(missing)}", status_code=status.HTTP_400_BAD_REQUEST)

            email_id = data["emailId"]
            app_password = data["appPassword"]
            smtp_server = data["smtpServer"]
            smtp_port = int(data["smtpPort"])

            # Step 1: Verify the hostname resolves before attempting SMTP
            try:
                socket.getaddrinfo(smtp_server, smtp_port)
            except socket.gaierror:
                return error_response(
                    message=f"Cannot resolve SMTP server hostname '{smtp_server}'. Please check the server address.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Step 2: Try sending test email
            try:
                msg = MIMEText("This is a test email to verify SMTP settings.")
                msg["Subject"] = "SMTP Settings Verification"
                msg["From"] = email_id
                msg["To"] = email_id

                if smtp_port == 465:
                    server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
                else:
                    server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
                    server.starttls()
                server.login(email_id, app_password)
                server.sendmail(email_id, [email_id], msg.as_string())
                server.quit()
            except Exception as smtp_err:
                print(f"SMTP connection failed for {smtp_server}:{smtp_port} — {str(smtp_err)}")
                return error_response(
                    message=f"SMTP connection failed ({smtp_server}:{smtp_port}): {str(smtp_err)}",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Step 2: Update DB only if test email succeeded
            update_query = """
                UPDATE "emailSettings"
                SET "emailId"=%s, "appPassword"=%s, "smtpServer"=%s, "smtpPort"=%s
                WHERE "isDeleted" = 0
                RETURNING id
            """
            params = [email_id, app_password, smtp_server, smtp_port]
            result = execute_query(update_query, params, fetch="one")

            if not result:
                return error_response(message="Failed to update email", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            return success_response(data=result[0], message="Email updated successfully", status_code=status.HTTP_200_OK)

        except Exception as e:
            return error_response(message=f"Error updating email: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.core.cache import cache
from django.conf import settings
import base64
import psycopg2
from rest_framework import status
from api.utils import success_response, error_response, render_template_text, execute_query, send_mail_with_template, send_email_via_db_config


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("userName")
        if not username:
            return error_response(message=USERNAME_IS_REQUIRED, status_code=status.HTTP_200_OK)

        cache_key = f"forgot_password_attempts_{username}"
        attempts = cache.get(cache_key, 0)
        if attempts >= 3:
            return error_response(
                message="You have reached the limit for forgetting password. Please try again later.",
                status_code=status.HTTP_200_OK
            )

        conn = None
        try:
            # Connect to Postgres manually
            conn = psycopg2.connect(
                dbname=settings.DATABASES['default']['NAME'],
                user=settings.DATABASES['default']['USER'],
                password=settings.DATABASES['default']['PASSWORD'],
                host=settings.DATABASES['default']['HOST'],
                port=settings.DATABASES['default']['PORT']
            )
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT "user"."userName", "user"."phoneNumber", "user"."userPassword"
                    FROM "public"."user"
                    WHERE "user"."userName" = %s
                """, (username,))
                user_record = cursor.fetchone()

            if not user_record:
                return error_response(message=USERNAME_DOES_NOT_EXIST, status_code=status.HTTP_200_OK)

            user_name_db, user_email, encoded_password = user_record

            try:
                decoded_password = base64.b64decode(encoded_password).decode()
            except Exception:
                return error_response(message="Error processing user data.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # 🔑 Fetch Forgot Password template
            template_query = """
                SELECT subject, body
                FROM "mailTemplates"
                WHERE "templateName" = %s AND "isDeleted" = 0
                LIMIT 1
            """
            template = execute_query(template_query, ["Forgot Password"], fetch="one")
            if not template:
                return error_response("Forgot Password email template not found", status.HTTP_500_INTERNAL_SERVER_ERROR)

            subject, body = template

            # 🔑 Build dynamic context
            context = {
                "User Name": user_name_db,
                "Password": decoded_password
            }

            # 🔑 Replace placeholders dynamically
            subject = render_template_text(subject, context)
            body = render_template_text(body, context)

            # 🔑 Send email
            send_email_via_db_config(user_email, subject, body)

            # Limit attempts
            cache.set(cache_key, attempts + 1, timeout=3600)

            return success_response(message=PASSWORD_SENT_TO_EMAIL_SUCCESSFULLY, status_code=status.HTTP_200_OK)

        except psycopg2.Error as db_err:
            return error_response(message=f"Database error: {str(db_err)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return error_response(message=f"An error occurred: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        finally:
            if conn:
                conn.close()


class CustomerForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        customer_email = request.data.get("email")

        if not customer_email:
            return error_response(message=USERNAME_IS_REQUIRED, status_code=status.HTTP_200_OK)

        cache_key = f"forgot_password_attempts_{customer_email}"
        attempts = cache.get(cache_key, 0)

        if attempts >= 3:
            return error_response(
                message="You have reached the limit for forgetting password. Please try again later.",
                status_code=status.HTTP_200_OK
            )

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
                    SELECT "customerName", "email", "password"
                    FROM "public"."customer" 
                    WHERE "email" = %s AND "isDeleted" = 0
                """, (customer_email,))
                customer_record = cursor.fetchone()

            if not customer_record:
                return error_response(message=USERNAME_DOES_NOT_EXIST, status_code=status.HTTP_200_OK)

            customer_name_db, customer_email, encoded_password = customer_record

            try:
                decoded_password = base64.b64decode(encoded_password).decode()
            except Exception:
                return error_response(message="Error processing customer data.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # 🔑 Prepare context for template placeholders
            context = {
                "Customer Name": customer_name_db,
                "Email": customer_email,
                "Password": decoded_password
            }

            # 📩 Send mail using DB template + SMTP credentials
            send_mail_with_template("Customer Forgot Password", customer_email, context)

            cache.set(cache_key, attempts + 1, timeout=3600)  # 1 hour timeout

            return success_response(message=PASSWORD_SENT_TO_EMAIL_SUCCESSFULLY, status_code=status.HTTP_200_OK)

        except psycopg2.Error as db_err:
            return error_response(message=f"Database error: {str(db_err)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return error_response(message=f"An error occurred: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        finally:
            if conn:
                conn.close()

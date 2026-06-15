import base64
from types import SimpleNamespace
from django.utils import timezone

# Django REST Framework Imports
from rest_framework.views import APIView
from rest_framework import status

# Simple JWT Imports
from rest_framework_simplejwt.tokens import RefreshToken

# Your Custom Utility Imports
# Ensure the paths to these files are correct for your project structure
from api.utils import success_response, error_response, execute_query, log_activity_raw
from rest_framework.permissions import AllowAny, IsAuthenticated


class CustomerLoginView(APIView):
    """
    Handles customer login using email and Base64 password check and issues stateless JWT tokens.
    """
    permission_classes = [AllowAny] # Anyone can attempt to log in.

    def post(self, request):
        email_from_request = request.data.get("emailId")
        input_password = request.data.get("password")

        if not email_from_request or not input_password:
            return error_response(message="Email and password are required.", status_code=status.HTTP_400_BAD_REQUEST)

        try:
            query = 'SELECT * FROM customer WHERE email = %s AND "isDeleted" = 0'
            result_list = execute_query(query, [email_from_request], fetch='one')

            if not result_list:
                return error_response(message="Unable to log in with provided credentials.", status_code=status.HTTP_401_UNAUTHORIZED)

            customer_data = result_list[0]

            # Verify Password using Base64 Decode
            try:
                stored_base64_password = customer_data.get('password')
                if not stored_base64_password:
                    raise ValueError("Stored password in database is null or empty.")
                stored_decoded_password = base64.b64decode(stored_base64_password.encode('utf-8')).decode('utf-8')
            except Exception as e:
                print(f"CRITICAL: Could not decode stored password for email '{email_from_request}'. Error: {e}")
                return error_response(message="Internal server error during authentication.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            if stored_decoded_password != input_password:
                return error_response(message="Unable to log in with provided credentials.", status_code=status.HTTP_401_UNAUTHORIZED)

            if customer_data.get('status') != 0:
                return error_response(message="This customer account is inactive.", status_code=status.HTTP_403_FORBIDDEN)

            # Manually Create a Refresh Token (Stateless)
            refresh = RefreshToken()

            # CRITICAL: Add the 'user_id' claim that the authenticator expects.
            # Its value is the customer's primary key (id).
            refresh['user_id'] = customer_data['id']
            
            # Add any other custom claims you want for convenience
            refresh['customerId'] = customer_data['customerId']

            refresh['auth_type'] = 'customer_access'

            # Update Last Login Time
            execute_query('UPDATE customer SET "lastLogin" = %s WHERE id = %s', [timezone.now(), customer_data['id']])

            response_data = {
                "customerData": {
                    "id": customer_data['id'],
                    "customerId": customer_data['customerId'],
                    "customerName": customer_data['customerName'],
                    "email": customer_data['email'],
                    "contactNumber": customer_data['contactNumber'],
                    "emirate": customer_data['emirate']
                },
                "refreshToken": str(refresh),
                "jwt": str(refresh.access_token)
            }
            performer = SimpleNamespace(
                id=customer_data['id'],
                fullName=customer_data.get('customerName', 'N/A') # Map customerName to fullName
            )
            log_activity_raw(
                request,
                category='Authentication',
                action='Customer Login',
                performer=performer  # Pass the object, not the ID
            )
            return success_response(data=response_data, message="Logged in successfully", status_code=status.HTTP_200_OK)
        

        except Exception as e:
            print(f"UNEXPECTED LOGIN ERROR for email '{email_from_request}': {e}")
            return error_response(message="An internal error occurred during login.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

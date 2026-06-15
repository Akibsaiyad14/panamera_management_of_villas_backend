from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import status
from rest_framework_simplejwt.exceptions import TokenError
from api.utils import success_response, error_response, execute_query, log_activity_raw

# class LogoutView(APIView):
#     def post(self, request):
#         refresh_token = request.data.get("refresh")
#         if not refresh_token:
#             refresh_token = request.data.get("refreshToken")
#         if not refresh_token:
#             return error_response(message="Refresh token is required.", status_code=status.HTTP_400_BAD_REQUEST)
#         try:
#             token = RefreshToken(refresh_token)
#             token.blacklist()
#             return success_response(data=None, message="Logged out successfully", status_code=status.HTTP_200_OK)
#         except TokenError:
#             return error_response(message="Invalid or expired token.", status_code=status.HTTP_400_BAD_REQUEST)
#         except Exception:
#             return error_response(message="Logout failed due to an unexpected error.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



class LogoutView(APIView):
    """
    Handles user logout for an AUTHENTICATED user.
    This is the recommended approach for better security and simplicity.
    """
    permission_classes = [IsAuthenticated] # Protect the endpoint

    def post(self, request):
        # The user is already authenticated via their access token.
        user = request.user

        refresh_token = request.data.get("refresh")
        if not refresh_token:
            refresh_token = request.data.get("refreshToken")

        if not refresh_token:
            return error_response(message="Refresh token is required.", status_code=status.HTTP_400_BAD_REQUEST)

        try:
            # --- Update the database to remove the FCM token ---
            # We use the authenticated user's ID directly from the request.
            print(f"Attempting to remove FCM token for user: {user.userName} (ID: {user.id})")
            execute_query(
                """UPDATE "user" SET "fcmToken" = NULL WHERE id = %s""",
                [user.id]
            )
            print(f"Successfully removed FCM token for user ID: {user.id}")

            # Blacklist the provided refresh token
            token = RefreshToken(refresh_token)
            token.blacklist()

            log_activity_raw(
                request=request,
                category='Authentication',
                action='Logout',
                performer=user,
                target_employee_name=user.fullName,
                details={
                    'userName': user.fullName
                }
            )


            return success_response(data=None, message="Logged out successfully", status_code=status.HTTP_200_OK)

        except TokenError:
            return error_response(message="Invalid or expired refresh token.", status_code=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"ERROR during logout for user {user.id}: {str(e)}")
            return error_response(message="Logout failed due to an unexpected error.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

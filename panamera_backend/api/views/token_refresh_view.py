from api.messages import *
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework import status
from django.conf import settings
import jwt
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
# from rest_framework_simplejwt.exceptions import TokenError
from api.utils import success_response, error_response

User = get_user_model()

class CustomTokenRefreshView(TokenRefreshView):
    """Enhanced Token Refresh View with better error handling and security"""

    def post(self, request, *args, **kwargs):
        refresh_token_from_request = request.data.get("refreshToken")

        if not refresh_token_from_request:
            return error_response(
                message=REFRESH_TOKEN_REQUIRED,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Verify token signature first
            decoded_token = jwt.decode(
                refresh_token_from_request,
                settings.SECRET_KEY,
                algorithms=["HS256"]
            )

            # Check if token is blacklisted
            from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
            if BlacklistedToken.objects.filter(token__token=refresh_token_from_request).exists():
                return error_response(
                    message="This refresh token has been blacklisted. Please log in again.",
                    status_code=status.HTTP_401_UNAUTHORIZED
                )

            # Prepare request for parent class
            request.data['refresh'] = refresh_token_from_request

            try:
                response_from_super = super().post(request, *args, **kwargs)

                if response_from_super.status_code == status.HTTP_200_OK:
                    new_access_token = response_from_super.data.get("access")
                    # Get new refresh token if rotation happened
                    new_refresh_token = response_from_super.data.get("refresh", refresh_token_from_request)

                    payload = {
                        "jwt": new_access_token,
                        "refreshToken": new_refresh_token
                    }
                    return success_response(
                        data=payload,
                        message="Access token refreshed successfully.",
                        status_code=status.HTTP_200_OK
                    )
                else:
                    error_message = response_from_super.data.get("detail", TOKEN_REFRESH_FAILED)
                    return error_response(
                        message=error_message,
                        status_code=response_from_super.status_code
                    )
            finally:
                # Clean up the request data
                request.data.pop('refresh', None)

        except jwt.ExpiredSignatureError:
            # Special handling for expired tokens
            try:
                # Decode without expiration check
                decoded_token = jwt.decode(
                    refresh_token_from_request,
                    settings.SECRET_KEY,
                    algorithms=["HS256"],
                    options={"verify_exp": False}
                )

                user_id = decoded_token.get("user_id")
                if not user_id:
                    return error_response(
                        message="Invalid expired refresh token: user ID not found",
                        status_code=status.HTTP_401_UNAUTHORIZED
                    )

                # Verify user exists
                user = User.objects.get(id=user_id)

                # Generate new token pair
                refresh = RefreshToken.for_user(user)
                payload = {
                    "jwt": str(refresh.access_token),
                    "refreshToken": str(refresh)
                }

                # Blacklist the old token
                from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
                try:
                    old_token = OutstandingToken.objects.get(token=refresh_token_from_request)
                    BlacklistedToken.objects.create(token=old_token)
                except OutstandingToken.DoesNotExist:
                    pass

                return success_response(
                    data=payload,
                    message="Tokens refreshed successfully (new pair generated).",
                    status_code=status.HTTP_200_OK
                )

            except User.DoesNotExist:
                return error_response(
                    message=USER_NOT_FOUND_FOR_EXPIRED_TOKEN,
                    status_code=status.HTTP_401_UNAUTHORIZED
                )
            except Exception as e:
                return error_response(
                    message=f"Error processing expired token: {str(e)}",
                    status_code=status.HTTP_401_UNAUTHORIZED
                )

        except jwt.InvalidTokenError:
            return error_response(
                message="Invalid refresh token.",
                status_code=status.HTTP_401_UNAUTHORIZED
            )
        except Exception as e:
            return error_response(
                message=f"An unexpected error occurred during token refresh: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

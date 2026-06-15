from rest_framework.views import APIView
from rest_framework import status
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from api.utils import success_response, log_activity_raw, execute_query
from types import SimpleNamespace
from api.views.customer_authentication import CustomCustomerAuthentication 

class CustomerLogoutView(APIView):
    """
    Handles customer logout by invalidating the access token, blacklisting the refresh token,
    logging the logout event, and returning a success message.
    """
    authentication_classes = [CustomCustomerAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Invalidates the current customer session and logs the 'Customer Logout' event.
        """
        try:
            customer_id = request.user.id
            customer_name = getattr(request.user, "customerName", None) or "Unknown Customer"

            refresh_token = request.data.get("refreshToken") or request.data.get("refresh")

            if refresh_token:
                try:
                    token = RefreshToken(refresh_token)
                    token.blacklist()
                except Exception as exc:
                    print(f"Warning: could not blacklist customer refresh token during logout: {exc}")

            execute_query(
                'UPDATE customer SET "token_invalidated_at" = %s, "fcmToken" = NULL WHERE id = %s',
                [timezone.now(), customer_id],
                fetch=False,
            )

            query = 'SELECT "customerName" FROM customer WHERE id = %s'
            result = execute_query(query, [customer_id], fetch='one')

            if result:
                customer_name = result[0].get('customerName')

            performer = SimpleNamespace(
                id=customer_id,
                fullName=customer_name
            )

            log_activity_raw(
                request,
                category='Authentication',
                action='Customer Logout',
                performer=performer
            )
        except Exception as e:
            print(f"Error while logging customer logout: {e}")
        
        return success_response(
            message="Logout successfully.",
            status_code=status.HTTP_200_OK
        )

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query
from api.views.customer_authentication import CustomCustomerAuthentication



class StoreFCMTokenView(APIView):
    """
    Receives and stores the FCM registration token for the authenticated user.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_id = request.user.id
        token = request.data.get("fcmToken")

        if not token:
            return error_response(message="FCM token is required.", status_code=400)

        # Update the user's record with the new FCM token
        execute_query("""
            UPDATE "user" SET "fcmToken" = %s WHERE id = %s
        """, [token, user_id], fetch=False)

        return success_response(message="FCM token updated successfully.")


class CustomerStoreFCMTokenView(APIView):
    """
    Receives and stores the FCM registration token for the authenticated customer.
    """
    # CRITICAL FIX: Specify the custom authentication class for this view.
    authentication_classes = [CustomCustomerAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # The authenticated customer object is now available in `request.user`
        # thanks to your CustomCustomerAuthentication class.
        authenticated_customer_id = request.user.id
        token = request.data.get("fcmToken")

        if not token:
            return error_response(message="FCM token is required.", status_code=status.HTTP_400_BAD_REQUEST)

        try:
            # Update the customer's record with the new FCM token.
            # There's no need to re-verify if the customer exists,
            # the authentication class has already done that.

            # SQL FIX: The placeholder was incorrect (%JTI should be %s).
            query = 'UPDATE customer SET "fcmToken" = %s WHERE id = %s'
            params = [token, authenticated_customer_id]
            
            rows_affected = execute_query(query, params, fetch=False)

            if rows_affected == 0:
                 # This case might occur if the customer was deleted between token creation and this API call.
                 return error_response(message="Customer not found or could not be updated.", status_code=status.HTTP_404_NOT_FOUND)

            return success_response(message="FCM token updated successfully for customer.", status_code=status.HTTP_200_OK)

        except Exception as e:
            print(f"ERROR updating FCM token for customer id '{authenticated_customer_id}': {e}")
            return error_response(message="An internal error occurred while updating FCM token.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
import traceback
from ..utils import error_response, success_response, execute_query
from ..constants import *



class PaymentKeyPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):

        try:
            
            query = '''SELECT "key", "password" FROM public."serviceCredentials"'''
            result = execute_query(query, fetch=True)
            if result:
                payment_key_password = result[0]
                return success_response(message="Payment key password retrieved successfully",status_code=status.HTTP_200_OK, data=payment_key_password)
            else:
                return error_response(message="Customer not found for the user", status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            traceback.print_exc()
            return error_response(message="An error occurred while retrieving payment key password", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
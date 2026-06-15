from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query

class FunctionalityListView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            query = """
                SELECT "functionId", "functionName","functionKey", "functionType"
                FROM "functionalityList"
                WHERE "isDeleted" = 0
                ORDER BY "functionId" ASC
            """
            functionalities = execute_query(query, many=True)

            return success_response(
                data=functionalities,
                message="Functionality list fetched successfully"
            )
        except Exception as e:
            return error_response(
                message=f"Error fetching functionality list: {str(e)}",
                status_code=500
            )

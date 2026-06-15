from datetime import datetime, timedelta
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response



class ReportViewsDateTimeAPIView(APIView):
    """
    API view to get the last updated dateTime for report views.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            query = """
                SELECT *
                FROM public."reportsViews"
                
            """
            result = execute_query(query, [], many=True)
            if result:
            # if result and result[0]['dateTime']:
            #     last_updated = result[0]['dateTime']
                return success_response(message="result fetched successfully", data=result)
            else:
                return error_response(message="No dateTime found for report views.", status_code=404)
        except Exception as e:
            return error_response(message=  f"An error occurred: {str(e)}", status_code=500)

# from messages import *
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from api.utils import execute_query, success_response, error_response


class EnvironmentURLView(APIView):
    # permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            query = """
                SELECT id, environment_urls, "createdAt"
                FROM environment_url_view
                ORDER BY "createdAt" DESC;
            """
            data = execute_query(query)
            return success_response(
                data=data,
                message="Environment URLs fetched successfully"
            )
        except Exception as e:
            return error_response(
                message=f"Error fetching environment URLs: {str(e)}",
                status_code=500
            )

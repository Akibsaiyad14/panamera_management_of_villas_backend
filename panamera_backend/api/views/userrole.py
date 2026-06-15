from messages import *
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

# Assuming these are already in your utils or helpers
from api.utils import execute_query, success_response, error_response


class RoleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            query = """
                SELECT "roleId", "roleName", "isTeamLeader"
                FROM "userrole"
                WHERE "isDeleted" = 0
                ORDER BY "roleName" ASC
            """
            roles = execute_query(query)

            return success_response(data=roles, message="Role list fetched successfully")
        except Exception as e:
            return error_response(message=f"Error fetching role list: {str(e)}", status_code=500)

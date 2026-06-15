from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query


class EmployeeByOrderRoleListView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data
        roleOrderId = data.get("roleOrderId")
        groupNumber = data.get("groupNumber")
        is_team_leader = data.get("isTeamLeader", False)

        if is_team_leader:
            query = """
                SELECT u.id, ur."roleName", u."employeeId", u."fullName", u."employmentStatus"
                FROM "user" AS u
                JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE ur."isTeamLeader" = true AND COALESCE(u."isDeleted", '0') = '0' AND u."employmentStatus" = 'Active'
                ORDER BY u."id" ASC
            """
            try:
                supervisors = execute_query(query, many=True)
                return success_response(
                    data=supervisors,
                    message=TEAM_LEADER_LIST_FETCHED_SUCCESSFULLY
                )
            except Exception as e:
                return error_response(
                    message=f"Error fetching team leader list: {str(e)}",
                    status_code=500,
                )
        # If neither roleOrderId nor groupNumber is provided, return error
        if not roleOrderId and not groupNumber:
            return error_response(
                message="Either roleOrderId or groupNumber is required.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # Build query based on provided parameter
            if groupNumber:
                query = """
                    SELECT u.id, ur."roleName",u."employeeId", u."fullName", u."employmentStatus"
                    FROM "user" AS u
                    JOIN "userrole" ur ON u."roleId" = ur."roleId"
                    WHERE ur."groupNumber" = %s AND u."isDeleted" = 0 AND u."employmentStatus" = 'Active'
                    ORDER BY u."id" ASC
                """
                params = [groupNumber]
            else:
                query = """
                    SELECT u.id, ur."roleName",u."employeeId", u."fullName", u."employmentStatus"
                    FROM "user" AS u
                    JOIN "userrole" ur ON u."roleId" = ur."roleId"
                    WHERE ur."groupNumber" = %s AND u."isDeleted" = 0 AND u."employmentStatus" = 'Active'
                    ORDER BY u."id" ASC
                """
                params = [roleOrderId]

            supervisors = execute_query(query, params, many=True)

            return success_response(
                data=supervisors,
                message=SUPERVISOR_LIST_FETCHED_SUCCESSFULLY
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching supervisor list: {str(e)}",
                status_code=500,
            )

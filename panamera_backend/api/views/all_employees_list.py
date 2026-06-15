from api.messages import (
    NO_EMPLOYEES_FOUND_MATCHING_CRITERIA,
    EMPLOYEE_LIST_FETCHED_SUCCESSFULLY,
)
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
import json
from rest_framework import status
from api.utils import success_response, error_response, execute_query



class AllEmployeeListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # Get functionalityKey from query parameters
            functionality_keys = request.query_params.get("functionalityKey", "").strip()

            where_clause = 'WHERE u."isDeleted" = 0'
            params = []

            # Add functionalityKey filter
            if functionality_keys:
                key_list = [key.strip() for key in functionality_keys.split(",") if key.strip()]
                if key_list:
                    where_clause += """ AND EXISTS (
                        SELECT 1
                        FROM userrole ur2
                        WHERE ur2."roleId" = u."roleId"
                        AND ur2."functionalityKey" @> %s
                    )"""
                    params.append(json.dumps(key_list))

            query = f"""
                SELECT
                    u.id, u."employeeId", u."fullName",
                    u."roleId", ur."roleName", ur."roleOrderId", ur."groupNumber"
                FROM "user" AS u
                JOIN userrole ur ON u."roleId" = ur."roleId"
                {where_clause}
                ORDER BY u."id" ASC
            """
            # print(f"Query: {query}")
            # print(f"Params: {params}")

            supervisors = execute_query(query, params, many=True)

            if not supervisors:
                return success_response(data=[], message=NO_EMPLOYEES_FOUND_MATCHING_CRITERIA)

            return success_response(data=supervisors, message=EMPLOYEE_LIST_FETCHED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=f"Error fetching employee list: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

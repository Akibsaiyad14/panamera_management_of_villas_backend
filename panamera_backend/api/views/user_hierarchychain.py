from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response
from rest_framework import status



class UserHierarchyView(APIView):
    """
    API view to retrieve the complete reporting hierarchy, using roleOrderId from the
    userrole table for sorting/identification. Includes employeeId from the user table.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        try:
            # --- 1. CONSTRUCT THE SQL QUERY ---
            query = """
                WITH RECURSIVE HierarchyChain AS (
                    -- Anchor Member: Find the direct manager and their roleOrderId.
                    SELECT
                        u.id,
                        u."fullName",
                        u."reportingToId",
                        u."employeeId",
                        ur."roleOrderId",
                        ur."roleName"
                    FROM
                        "user" AS u
                    LEFT JOIN
                        "userrole" ur ON u."roleId" = ur."roleId"
                    WHERE
                        u.id = (SELECT "reportingToId" FROM "user" WHERE id = %s)
                        AND u."isDeleted" = 0

                    UNION ALL

                    -- Recursive Member: Find the next manager up and their roleOrderId.
                    SELECT
                        u.id,
                        u."fullName",
                        u."reportingToId",
                        u."employeeId",
                        ur."roleOrderId",
                        ur."roleName"
                    FROM
                        "user" AS u
                    INNER JOIN
                        HierarchyChain hc ON u.id = hc."reportingToId"
                    LEFT JOIN
                        "userrole" ur ON u."roleId" = ur."roleId"
                    WHERE
                        u."isDeleted" = 0
                )
                -- Final Selection: Select all required fields, including employeeId.
                SELECT
                    id,
                    "fullName",
                    "roleOrderId",
                    "roleName",
                    "employeeId"
                FROM
                    HierarchyChain
                WHERE
                    "roleName" IS NOT NULL
                ORDER BY
                    "roleOrderId" ASC;
            """

            # --- 2. EXECUTE QUERY ---
            hierarchy = execute_query(query, [user_id], fetch=True, many=True)
            # logger.debug(f"Raw hierarchy result for user_id {user_id}: {hierarchy}")

            if not hierarchy:
                return success_response(data=[], message=f"No reporting hierarchy found for user ID {user_id}.")

            # --- 3. ASSEMBLE RESPONSE ---
            return success_response(data=hierarchy, message="User hierarchy fetched successfully")

        except Exception as e:
            # logger.error(f"Error fetching hierarchy for user_id {user_id}: {str(e)}", exc_info=True)
            return error_response(message=f"Error fetching user hierarchy: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

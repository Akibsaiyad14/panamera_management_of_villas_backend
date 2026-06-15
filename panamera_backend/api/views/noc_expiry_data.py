from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query
from api.messages import *


class NOCExpiryView(APIView):
    """
    Read-only API to list AMC jobs with their NOC expiry dates.
    Accessible only to Super Admin (roleId = 1).
    Results are sorted by NOC expiry date ascending (soonest first).
    """
    permission_classes = [IsAuthenticated]

    allowed_sort_fields = ["nocExpiryDate"]

    def get(self, request):
        try:
            
            # Query params
            search = request.query_params.get("search", "").strip()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            if page < 1:
                page = 1
            if page_size < 1:
                page_size = 20
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            sort_param = request.query_params.get("sort", "").strip()
            # Optional filter: only show items expiring within N days
            expiring_within_days = request.query_params.get("expiringWithinDays", "").strip()

            where_clauses = ['m."isDeleted" = 0', 'm."nocExpiryDate" IS NOT NULL']
            params = []

            if search:
                where_clauses.append("""(
                    m."amcJobName" ILIKE %s OR
                    c."customerName" ILIKE %s
                )""")
                search_param = f"%{search}%"
                params.extend([search_param, search_param])

            if expiring_within_days:
                try:
                    days = int(expiring_within_days)
                    where_clauses.append('m."nocExpiryDate" <= CURRENT_DATE + INTERVAL \'1 day\' * %s')
                    params.append(days)
                except ValueError:
                    pass

            where_clause = "WHERE " + " AND ".join(where_clauses)

            # Count
            count_query = f"""
                SELECT COUNT(*) AS total
                FROM "AMCMaster" m
                LEFT JOIN "customer" c
                    ON c."customerId" = m."customerId" AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "villaDetails" v
                    ON v."id" = m."villaId" AND COALESCE(v."isDeleted", 0) = 0
                {where_clause}
            """
            total_result = execute_query(count_query, list(params), fetch='one')
            if isinstance(total_result, dict):
                total_count = total_result.get("total", 0)
            elif isinstance(total_result, list) and total_result:
                total_count = total_result[0].get("total", 0)
            else:
                total_count = 0

            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0


            order_by = 'ORDER BY m."nocExpiryDate" ASC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    order_by = f'ORDER BY m."{sort_field}" {sort_direction}'

            # Main query — soonest expiry first by default
            query = f"""
                SELECT
                    m."amcId",
                    m."jobId",
                    m."amcJobName",
                    v."id" AS "villaId",
                    v."villaName",
                    c."customerId",
                    c."customerName",
                    m."nocExpiryDate"
                FROM "AMCMaster" m
                LEFT JOIN "customer" c
                    ON c."customerId" = m."customerId" AND COALESCE(c."isDeleted", 0) = 0
                LEFT JOIN "villaDetails" v
                    ON v."id" = m."villaId" AND COALESCE(v."isDeleted", 0) = 0
                {where_clause}
                {order_by}
            """

            paginated_params = list(params)
            if not is_export:
                query += " LIMIT %s OFFSET %s"
                paginated_params.extend([page_size, (page - 1) * page_size])

            results = execute_query(query, paginated_params, many=True)

            response_data = {
                "results": results,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                }
            }

            return success_response(
                data=response_data,
                message="NOC expiry list fetched successfully.",
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching NOC expiry list: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

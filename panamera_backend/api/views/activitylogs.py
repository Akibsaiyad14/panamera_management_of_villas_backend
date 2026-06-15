
from rest_framework.views import APIView
from rest_framework import status
from api.utils import execute_query, success_response, error_response
from api.messages import (
    INVALID_PAGE_OR_PAGE_SIZE_PARAM,
    ACTIVITY_LOGS_FETCHED_SUCCESSFULLY,
    ERROR_FETCHING_ACTIVITY_LOGS,
)
from django.core.paginator import Paginator, EmptyPage


class ActivityLogsListAPI(APIView):
    """
    API endpoint to fetch a paginated and filterable list of activity logs.

    - Supports a global 'search' across multiple text fields.
    - Supports specific filtering by individual columns.
    - Supports pagination via 'page' and 'page_size' parameters.

    Examples:
    /api/activity-logs?search=Login
    /api/activity-logs?search=E07&device=Web
    /api/activity-logs?eventCategory=Attendance&page=2
    """
    def get(self, request):
        try:
            # --- 1. DYNAMIC QUERY BUILDING ---

            where_clauses = []
            filter_params = []

            # --- Global Search Logic ---
            search_term = request.query_params.get('search', None)
            if search_term:
                # Define which columns the global search should apply to.
                searchable_columns = [
                    '"logMessage"', '"eventCategory"', '"eventAction"',
                    '"userName"', '"employeeId"', '"device"',
                    '"macId"', '"appVersion"'
                ]

                # Create a list of "column ILIKE %s" for each searchable column
                or_conditions = [f"{col} ILIKE %s" for col in searchable_columns]

                # Join them with OR and wrap in parentheses to form a single search block
                search_clause = "(" + " OR ".join(or_conditions) + ")"
                where_clauses.append(search_clause)

                # Add the search term to the params list for each OR condition
                filter_params.extend([f'%{search_term}%'] * len(searchable_columns))

            # --- Specific Column Filter Logic ---
            # This is a security measure to prevent arbitrary column filtering.
            allowed_filters = {
                'id': '"id"',
                'eventCategory': '"eventCategory"',
                'eventAction': '"eventAction"',
                'userId': '"userId"',
                'userName': '"userName"',
                'employeeId': '"employeeId"',
                'shiftId': '"shiftId"',
                'device': '"device"',
                'macId': '"macId"',
                'appVersion': '"appVersion"',
            }

            for param, column_name in allowed_filters.items():
                if param in request.query_params:
                    operator = 'ILIKE' if param not in ['id', 'userId'] else '='
                    where_clauses.append(f"{column_name} {operator} %s")

                    value = request.query_params[param]
                    filter_params.append(f'%{value}%' if operator == 'ILIKE' else value)

            # Combine all WHERE clauses with "AND"
            where_sql = ""
            if where_clauses:
                where_sql = "WHERE " + " AND ".join(where_clauses)

            # --- 2. PAGINATION SETUP ---
            try:
                page_number = int(request.query_params.get('page', 1))
                page_size = int(request.query_params.get('page_size', 20))
                if page_number < 1 or page_size < 1: raise ValueError
            except ValueError:
                return error_response(INVALID_PAGE_OR_PAGE_SIZE_PARAM, status.HTTP_400_BAD_REQUEST)

            # --- 3. EXECUTE QUERIES ---

            # Get the total count of records matching all filters
            count_query = f'SELECT COUNT(*) as total FROM "activityLogs" {where_sql}'
            count_result = execute_query(count_query, params=filter_params, fetch=True, many=False)
            total_count = count_result[0]['total'] if count_result else 0

            # --- 4. SMART PAGE RESET LOGIC ---
            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            # If requested page > total available pages, reset to Page 1
            if total_pages > 0 and page_number > total_pages:
                page_number = 1
            if total_pages == 0:
                page_number = 1

            # --- 5. CALCULATE OFFSET & RUN DATA QUERY ---
            offset = (page_number - 1) * page_size
            data_query = f"""
                SELECT
                    id, "logTimestamp", "eventCategory", "eventAction",
                    "userId", "userName", "employeeId", "shiftId", device,
                    "macId", "appVersion", "logMessage"
                FROM "activityLogs"
                {where_sql}
                ORDER BY "logTimestamp" DESC
                LIMIT %s OFFSET %s
            """

            # Combine filter params with pagination params
            final_params = filter_params + [page_size, offset]
            logs = execute_query(data_query, params=final_params, fetch=True, many=True)

            total_pages = (total_count + page_size - 1) // page_size  # Calculate total pages

            # --- 4. FORMAT THE RESPONSE ---
            processed_logs = []
            for log in logs:
                formatted_log = {
                    "logMessage": log.get("logMessage"),
                    "id": log.get("id"),
                    "logTimestamp": log.get("logTimestamp").isoformat() if log.get("logTimestamp") else None,
                    "eventCategory": log.get("eventCategory"),
                    "eventAction": log.get("eventAction"),
                    "userId": log.get("userId"),
                    "userName": log.get("userName"),
                    "employeeId": log.get("employeeId"),
                    "shiftId": log.get("shiftId"),
                    "device": log.get("device"),
                    "macId": log.get("macId"),
                    "appVersion": log.get("appVersion")
                }
                processed_logs.append(formatted_log)

            # Build the final paginated response structure
            paginated_response = {
                'results': processed_logs,
                "pagination":{
                'totalRecords': total_count,
                'totalPages': total_pages,
                'currentPage': page_number,
                'pageSize': page_size}

            }

            return success_response(
                message=ACTIVITY_LOGS_FETCHED_SUCCESSFULLY,
                data=paginated_response,
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            print(f"Error in ActivityLogsListAPI: {e}")
            # logger.error(f"Error in ActivityLogsListAPI: {e}", exc_info=True)
            return error_response(
                message=ERROR_FETCHING_ACTIVITY_LOGS,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

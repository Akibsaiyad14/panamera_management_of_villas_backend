import traceback
from django.conf import settings
from rest_framework.views import APIView
from datetime import datetime, timedelta
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, log_activity_raw


class FeedbackView(APIView):
    """
    CRUD API for feedback table.
    Stores feedback linked to customer, fetches customer name on creation.
    """
    permission_classes = [IsAuthenticated]
    allowed_sort_fields = [
        "id", "customerId", "customerName", "feedbackText", "createdAt"
    ]

    def get(self, request, feedback_id=None):
        """
        Get single feedback (by id) or list all with pagination and search functionality.
        Includes customer name via join.
        """
        try:
            if feedback_id:
                query = '''
                    SELECT f.id, f."customerId", c."customerName", c."contactNumber",
                           f."feedbackText", f."createdAt"
                    FROM public.feedback f
                    JOIN public.customer c ON f."customerId" = c.id
                    WHERE f.id = %s;
                '''
                result = execute_query(query, [feedback_id], fetch='one')
                if not result:
                    return error_response(message="Feedback not found.", status_code=status.HTTP_404_NOT_FOUND)

                # Handle both dict and list return types
                if isinstance(result, dict):
                    pass
                elif isinstance(result, list) and result:
                    result = dict(zip(["id", "customerId", "customerName", "contactNumber", "feedbackText", "createdAt"], result))
                else:
                    return error_response(message="Feedback not found.", status_code=status.HTTP_404_NOT_FOUND)

                return success_response(data=result, message="Feedback retrieved successfully.", status_code=status.HTTP_200_OK)

            # list all with pagination and search
            search = request.query_params.get("search", "").strip()
            start_date = request.query_params.get("startDate", None)
            end_date = request.query_params.get("endDate", None)
            customer_id_filter = request.query_params.get("customerId", None)
            sort_param = request.query_params.get("sort", "").strip()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))

            where_conditions = []
            params = []

            if search:
                where_conditions.append(""" (c."customerName" ILIKE %s OR f."feedbackText" ILIKE %s) """)
                search_param = f"%{search}%"
                params.extend([search_param, search_param])

            if start_date:
                where_conditions.append('f."createdAt" >= %s')
                params.append(start_date)  # Assumes YYYY-MM-DD; Postgres handles as start of day for timestamps

            if end_date:
                where_conditions.append('f."createdAt" <= %s')
                # Add up to end of day
                end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
                params.append(end_dt.strftime('%Y-%m-%d %H:%M:%S'))

            if customer_id_filter:
                where_conditions.append('f."customerId" = %s')
                params.append(int(customer_id_filter))

            where_clause = 'WHERE ' + ' AND '.join(where_conditions) if where_conditions else ''

            # Construct sort expression
            sort_expr = 'f."createdAt" DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                
                if sort_field in self.allowed_sort_fields:
                    if sort_field == "customerName":
                        sort_expr = f'LOWER(TRIM(c."customerName")) {sort_direction}, f."createdAt" {sort_direction}'
                    elif sort_field == "createdAt":
                        sort_expr = f'f."{sort_field}" {sort_direction}'
                    else:
                        sort_expr = f'f."{sort_field}" {sort_direction}, f."createdAt" {sort_direction}'

            order_by = f'ORDER BY {sort_expr}'

            count_query = f'''
                SELECT COUNT(f.id) AS total 
                FROM public.feedback f
                JOIN public.customer c ON f."customerId" = c.id
                {where_clause}
            '''
            total_result = execute_query(count_query, params, fetch='one')
            
            total_count = 0
            if isinstance(total_result, list) and total_result:
                total_count = total_result[0]["total"] if isinstance(total_result[0], dict) else total_result[0][0]
            elif isinstance(total_result, dict):
                total_count = total_result["total"]
            
            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            query = f'''
                SELECT f.id, f."customerId", c."customerName", c."contactNumber",
                       f."feedbackText", f."createdAt"
                FROM public.feedback f
                JOIN public.customer c ON f."customerId" = c.id
                {where_clause}
                {order_by}
                LIMIT %s OFFSET %s;
            '''
            query_params = list(params)
            query_params.extend([page_size, (page - 1) * page_size])
            
            results = execute_query(query, query_params, many=True)

            # Define column names for mapping
            columns = ["id", "customerId", "customerName", "contactNumber", "feedbackText", "createdAt"]
            dict_results = []
            for row in results:
                if isinstance(row, list):
                    dict_results.append(dict(zip(columns, row)))
                else:
                    dict_results.append(row)

            response_data = {
                "results": dict_results,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                },
            }
            return success_response(data=response_data, message="Feedbacks retrieved successfully.", status_code=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error retrieving feedback: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def post(self, request):
        """
        Create new feedback.
        Fetches customer name from customer table and stores it.
        """
        try:
            required_fields = ["customerId", "feedbackText"]
            for field in required_fields:
                if field not in request.data:
                    return error_response(message=f"{field} is required.", status_code=status.HTTP_400_BAD_REQUEST)

            customer_id = request.data['customerId']
            feedback_text = request.data['feedbackText']

            # Fetch customer name
            customer_query = 'SELECT "customerName" FROM public.customer WHERE id = %s AND "isDeleted" = 0;'
            customer = execute_query(customer_query, [customer_id], fetch='one')
            if not customer:
                return error_response(message="Customer not found or deleted.", status_code=status.HTTP_404_NOT_FOUND)

            # Handle dict/list return type
            if isinstance(customer, dict):
                customer_name = customer.get("customerName")
            elif isinstance(customer, list) and customer:
                customer_name = customer[0].get("customerName")
            else:
                customer_name = None

            if not customer_name:
                return error_response(message="Customer name not found.", status_code=status.HTTP_404_NOT_FOUND)

            insert_query = '''
                INSERT INTO public.feedback ("customerId", "customerName", "feedbackText")
                VALUES (%s, %s, %s)
                RETURNING id, "customerId", "customerName", "feedbackText", "createdAt";
            '''
            result = execute_query(insert_query, [customer_id, customer_name, feedback_text], fetch='one')

            # Handle both dict and list return types
            if isinstance(result, dict):
                inserted = result
            elif isinstance(result, list) and result:
                inserted = result[0]
            else:
                return error_response(message="Failed to create feedback.", status_code=status.HTTP_400_BAD_REQUEST)

            preview_length = 150
            if len(feedback_text) > preview_length:
                feedback_preview = feedback_text[:preview_length] + "..."
            else:
                feedback_preview = feedback_text
            print("customer_name:", customer_name)
            # --- LOG THE CREATION WITH THE PREVIEW ---
            log_activity_raw(
                request,
                category='Feedback',
                action='Add',
                performer= None,
                details={
                    'customerName': customer_name,
                    'feedbackId': inserted,
                    'feedback_preview': feedback_preview
                }
            )


            return success_response(data=inserted, message="Feedback created successfully.", status_code=status.HTTP_201_CREATED)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error creating feedback: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, feedback_id=None):
        """
        Delete a feedback by ID.
        """
        try:
            if not feedback_id:
                return error_response(message="feedback_id required.", status_code=status.HTTP_400_BAD_REQUEST)

            # Check if exists
            check_query = 'SELECT id FROM public.feedback WHERE id = %s;'
            existing = execute_query(check_query, [feedback_id], fetch='one')
            if not existing:
                return error_response(message="Feedback not found.", status_code=status.HTTP_404_NOT_FOUND)

            # Delete from DB
            delete_query = 'DELETE FROM public.feedback WHERE id = %s RETURNING id;'
            deleted = execute_query(delete_query, [feedback_id], fetch='one')
            if not deleted:
                return error_response(message="Failed to delete feedback.", status_code=status.HTTP_400_BAD_REQUEST)

            return success_response(data={"id": feedback_id}, message="Feedback deleted successfully.", status_code=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error deleting feedback: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

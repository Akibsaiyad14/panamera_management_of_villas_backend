
from api.messages import *
import base64
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, encode_password, generate_password


class UserCredentialsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        try:
            query = """
                SELECT "userName", "userPassword", "employeeId"
                FROM "user"
                WHERE id = %s AND "isDeleted" = 0
            """
            params = [user_id]
            result = execute_query(query, params, fetch=True)

            if not result:
                return error_response(message="User not found", status_code=status.HTTP_404_NOT_FOUND)

            # Decode existing password
            encoded_password = result[0]["userPassword"]
            decoded_password = base64.b64decode(encoded_password).decode('utf-8')

            # Check for resetPassword flag
            reset_password_flag = request.query_params.get("resetPassword", "false").lower() == "true"

            if reset_password_flag:
                # if not request.user.has_perm('auth.change_user'):
                #     return error_response(message="You don't have permission to reset passwords", status_code=status.HTTP_403_FORBIDDEN)

                new_password = generate_password(3)
                encoded_new_password = encode_password(new_password)

                update_query = """
                    UPDATE "user"
                    SET "userPassword" = %s
                    WHERE id = %s AND "isDeleted" = 0
                    RETURNING id
                """
                update_params = [encoded_new_password, user_id]
                update_result = execute_query(update_query, update_params, fetch=True, many=False)

                if not update_result:
                    return error_response(message="User not found or already deleted", status_code=status.HTTP_404_NOT_FOUND)

                decoded_password = new_password  # overwrite password value in response

            # Build response payload
            user_data = {
                "userName": result[0]["userName"],
                # "employeeId": result[0]["employeeId"],
                "password": decoded_password
            }

            return success_response(data=user_data, message=USER_CREDENTIALS_FETCHED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=f"Error fetching credentials: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



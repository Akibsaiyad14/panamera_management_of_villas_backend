from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, error_response, success_response
from django.db import connection, DatabaseError


class NotificationListView(APIView):
    """
    Fetches notifications for the logged-in user or a given customer,
    depending on request parameters.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer_id = request.query_params.get("customerId")
        user_id = request.user.id if not customer_id else None

        # Build WHERE clause based on input
        if customer_id:
            where_clause = 'n."customerId" = %s'
            params = [customer_id]
        else:
            where_clause = 'n."userId" = %s'
            params = [user_id]

        query = f"""
            SELECT
                n."notificationId",
                n.title,
                n.body,
                n."dataPayload",
                n."isRead",
                n."createdAt",
                a.date as "attendanceDate"
            FROM "notifications" n
            LEFT JOIN "attendance" a ON n."attendanceId" = a.id
            WHERE {where_clause}
            AND (a."isDeleted" = 0 OR a.id IS NULL)
            ORDER BY n."notificationId" DESC
        """

        notifications = execute_query(query, params, many=True)

        return success_response(
            data=notifications,
            message="Notifications fetched successfully.",
            status_code=status.HTTP_200_OK
        )


class MarkNotificationsReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # 👇 Fix: match the key from your request
        notification_ids_to_mark = request.data.get("notificationId")

        try:
            with connection.cursor() as cursor:
                if notification_ids_to_mark and isinstance(notification_ids_to_mark, list):
                    if not all(isinstance(i, int) for i in notification_ids_to_mark):
                        return error_response(
                            message="notificationId must be a list of integers.",
                            status_code=status.HTTP_400_BAD_REQUEST,
                        )

                    cursor.execute(
                        """
                        UPDATE "notifications" 
                        SET "isRead" = true
                        WHERE "notificationId" IN %s
                          AND "isRead" = false
                        """,
                        [tuple(notification_ids_to_mark)],
                    )

                    updated_count = cursor.rowcount

                    if updated_count == 0:
                        return error_response(
                            message="No notifications matched the given IDs.",
                            status_code=status.HTTP_404_NOT_FOUND,
                        )

                    message = f"{updated_count} notification(s) marked as read."

                else:
                    return error_response(
                        message="notificationId must be provided as a list of integers.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

            connection.commit()
            return success_response(message=message, status_code=status.HTTP_200_OK)

        except DatabaseError as e:
            return error_response(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response



class CommunityListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            query = """
                SELECT DISTINCT community
                FROM "communityList"
                WHERE "isDeleted" = 0
                ORDER BY community;
            """

            rows = execute_query(query, fetch=True, many=True)

            # Convert list of dicts → list of strings
            communities = [row["community"] for row in rows]

            return success_response(
                message="Communities fetched successfully.",
                data=communities,
                status_code=status.HTTP_200_OK,
            )
        except Exception as e:
            return error_response(
                message="An error occurred while fetching communities.",
                error=str(e),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query

class SkillListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            query = """
                SELECT "skillName"
                FROM "SkillTest"
                ORDER BY "skillName" ASC
            """
            skills = execute_query(query, many=True)

            # Convert list of dicts to list of strings
            skill_list = [item["skillName"] for item in skills]

            return success_response(data=skill_list, message=SKILL_LIST_FETCHED_SUCCESSFULLY)
        except Exception as e:
            return error_response(message=f"Error fetching skill list: {str(e)}", status_code=500)


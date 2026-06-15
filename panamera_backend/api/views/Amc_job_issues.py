import json
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response, save_job_files
from api.messages import *

# Assume these helpers exist in your project
# from .utils import execute_query, success_response, error_response

class JobDayIssueView(APIView):
    """
    Handles listing (GET) and creating (POST) issues for a specific amcJobId.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, amcJobId):
        """
        GET /api/jobs/{amcJobId}/issues/
        Retrieves a complete list of all issues for a given job.
        """
        try:
            query = """
                SELECT
                    "issueId", "amcJobId", "logDate", "title", "severity",
                    "images", "audio", "issueDescription", "status",
                    "createdBy", "createdAt", "updatedBy", "updatedAt"
                FROM "JobDayIssues"
                WHERE "amcJobId" = %s
                ORDER BY "createdAt" DESC;
            """
            issues = execute_query(query, [amcJobId], many=True)

            # Format the response data
            response_data = []
            for issue in issues:
                response_data.append({
                    "issueId": issue["issueId"],
                    "amcJobId": issue["amcJobId"],
                    "logDate": issue["logDate"].strftime("%Y-%m-%d"),
                    "title": issue["title"],
                    "severity": issue["severity"],
                    "issueDescription": issue["issueDescription"],
                    "status": issue["status"],
                    "images": issue.get("images", []),
                    "audio": issue.get("audio", []),
                    "createdBy": issue["createdBy"],
                    "createdAt": issue["createdAt"].isoformat(),
                    "updatedBy": issue["updatedBy"],
                    "updatedAt": issue["updatedAt"].isoformat() if issue["updatedAt"] else None,
                })
            
            return success_response(data=response_data, message=ISSUES_FETCHED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request, amcJobId):
        """
        POST /api/jobs/{amcJobId}/issues/
        Creates a new issue and handles file uploads.
        """
        # With multipart/form-data, text data is in request.data
        data = request.data
        try:
            if not all(k in data for k in ['logDate', 'title']):
                return error_response(message="logDate and title are required fields.", status_code=status.HTTP_400_BAD_REQUEST)
            
            # Handle file uploads
            image_files = request.FILES.getlist('images')
            audio_files = request.FILES.getlist('audio')

            # Save files and get their paths
            image_paths = save_job_files(image_files, 'issues', amcJobId, data['logDate'])
            audio_paths = save_job_files(audio_files, 'issues', amcJobId, data['logDate'])
            
            query = """
                INSERT INTO "JobDayIssues"
                ("amcJobId", "logDate", "title", "severity", "issueDescription", "images", "audio", "status", "createdBy")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *;
            """
            params = [
                amcJobId,
                data['logDate'],
                data['title'],
                data.get('severity'),
                data.get('issueDescription'),
                json.dumps(image_paths),
                json.dumps(audio_paths),
                data.get('status', 0),
                request.user.userName  # <-- FIX #2: Corrected 'userName' to 'username'
            ]

            # `execute_query` returns a list, e.g., [{...}]
            new_issue_list = execute_query(query, params, many=False)
            
            # <-- FIX #1: THE MAIN FIX IS HERE -->
            # Check if the insert was successful and returned data
            if not new_issue_list:
                return error_response(message=FAILED_TO_CREATE_ISSUE_RECORD, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Get the dictionary from the list
            new_issue_dict = new_issue_list[0]

            image_urls = json.loads(new_issue_dict.get("images", "[]"))
            audio_urls = json.loads(new_issue_dict.get("audio", "[]"))


            response_data = {
                "issueId": new_issue_dict["issueId"],
                "date": new_issue_dict["logDate"].strftime("%Y-%m-%d"),
                "title": new_issue_dict["title"],
                "severity": new_issue_dict["severity"],
                "issueDescription": new_issue_dict["issueDescription"],
                "status": new_issue_dict["status"],
                "imageUrls": image_urls,
                "audioUrls": audio_urls,

            }

            # Now, build the response using the dictionary
            # response_data = {
            #     **new_issue_dict,
            #     "date": new_issue_dict["logDate"].strftime("%Y-%m-%d"),
            #     "createdAt": new_issue_dict["createdAt"].isoformat(),
            # }

            return success_response(
                data=response_data,
                message=ISSUE_CREATED_SUCCESSFULLY,
                status_code=status.HTTP_201_CREATED
            )

        except Exception as e:
            return error_response(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

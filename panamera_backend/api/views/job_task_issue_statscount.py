from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.db import connection
from api.utils import success_response, error_response, execute_query
from api.constants import *

# assuming your helper is in common/db_utils.py

class SupervisorJobStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Get count of AMC Jobs for a specific supervisor/team leader within a date range, and all approved Tasks and Issues.
        If the employeeId belongs to a team member, returns their team leader's stats.
        """
        try:
            employee_id = request.query_params.get("employeeId")
            start_date = request.query_params.get("startDate")
            end_date = request.query_params.get("endDate")

            if not (employee_id and start_date and end_date):
                return error_response(
                    message="employeeId, startDate, and endDate are required",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # Check if this employee is a team member and get their team leader if applicable
            team_check_query = """
                SELECT 
                    u."employeeId",
                    u."teamLeaderId",
                    COALESCE(u."fullName", u."userName") AS "userName",
                    tl."employeeId" AS "teamLeaderEmployeeId",
                    COALESCE(tl."fullName", tl."userName") AS "teamLeaderName"
                FROM "user" u
                LEFT JOIN "user" tl ON u."teamLeaderId" = tl.id
                WHERE u."employeeId" = %s AND COALESCE(u."isDeleted", 0) = 0
            """
            user_data = execute_query(team_check_query, [employee_id], many=False)
            
            if not user_data or not user_data[0]:
                return error_response(
                    message="Employee not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )
            
            user_info = user_data[0]
            
            # If user has a team leader, use team leader's employeeId for stats
            # Otherwise, use the original employeeId
            stats_employee_id = user_info.get('teamLeaderEmployeeId') if user_info.get('teamLeaderId') else employee_id
            display_name = user_info.get('teamLeaderName') if user_info.get('teamLeaderId') else user_info.get('userName')
            # is_team_member = bool(user_info.get('teamLeaderId'))

            query = """
                SELECT 
                    u."employeeId",
                    COALESCE(u."fullName", u."userName") AS "supervisorName",
                    COUNT(DISTINCT aj."amcJobId") AS "amcJobsCount",
                    (SELECT COUNT(DISTINCT id) FROM "taskManager" WHERE COALESCE("isDeleted", 0) = 0 AND "taskType" = 0 AND "approvalStatus" = 1 AND "taskStatus" !=1) AS "tasksCount",
                    (SELECT COUNT(DISTINCT id) FROM "taskManager" WHERE COALESCE("isDeleted", 0) = 0 AND "taskType" = 1 AND "approvalStatus" = 1 AND "taskStatus" !=1) AS "issuesCount",
                    (SELECT COUNT(DISTINCT id) FROM "emergencyRequest" WHERE COALESCE("isDeleted", 0) =0 AND "supervisorId" IS NOT NULL AND "paymentStatus" = %s) AS "esrCount"
                FROM "user" u
                LEFT JOIN "AMCMaster" m 
                    ON (
                        m."gardenSupervisorId" = u."employeeId"
                        OR m."poolSupervisorId" = u."employeeId"
                        OR m."gardenTeamLeaderId" = u."employeeId"
                        OR m."poolTeamLeaderId" = u."employeeId"
                    )
                    AND COALESCE(m."isDeleted", 0) = 0
                LEFT JOIN "AmcJobs" aj 
                    ON aj."amcId" = m."amcId"
                    AND aj."visitDate" BETWEEN %s AND %s
                    AND (
                        -- Garden jobs: only count if employee is garden supervisor or team leader
                        (aj."visitType" = 1 AND (m."gardenSupervisorId" = u."employeeId" OR m."gardenTeamLeaderId" = u."employeeId"))
                        OR
                        -- Pool jobs: only count if employee is pool supervisor or team leader
                        (aj."visitType" = 2 AND (m."poolSupervisorId" = u."employeeId" OR m."poolTeamLeaderId" = u."employeeId"))
                        OR
                        -- Combined jobs: count if employee is in any role
                        (aj."visitType" = 3 AND (m."gardenSupervisorId" = u."employeeId" OR m."gardenTeamLeaderId" = u."employeeId" OR m."poolSupervisorId" = u."employeeId" OR m."poolTeamLeaderId" = u."employeeId"))
                    )
                WHERE COALESCE(u."isDeleted", 0) = 0
                  AND u."employeeId" = %s
                GROUP BY u."employeeId", u."fullName", u."userName";
            """

            params = [PAYMENT_SUCCESS,start_date, end_date, stats_employee_id]

            result = execute_query(query, params=params, fetch=True, many=False)

            # execute_query returns a list (with one dict) or empty list
            data = result[0] if result else {
                "employeeId": stats_employee_id,
                "supervisorName": display_name,
                "amcJobsCount": 0,
                "tasksCount": 0,
                "issuesCount": 0,
                "esrCount": 0
            }

            return success_response(
                data=data,
                message="Supervisor job stats fetched successfully",
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching supervisor job stats: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

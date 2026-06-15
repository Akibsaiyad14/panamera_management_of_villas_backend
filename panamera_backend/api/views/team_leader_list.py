"""
Team Leader List API
Get list of all users with team leader role (isTeamleader = true)
"""
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response
from rest_framework import status as http_status


class TeamLeaderListView(APIView):
    """
    Get simple list of all team leaders.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        GET /api/teamLeaders/ - List all team leaders
        """
        try:
            # Get all team leaders with team status
            leaders_query = """
                SELECT 
                    u.id,
                    u."fullName",
                    u."employeeId",
                    u."phoneNumber",
                    u."roleId",
                    ur."roleName",
                    ur."roleOrderId",
                    CASE 
                        WHEN tm.id IS NOT NULL THEN true 
                        ELSE false 
                    END as "hasTeam",
                    tm.id as "teamId",
                    tm."teamName"
                FROM "user" u
                LEFT JOIN "userrole" ur ON u."roleId" = ur."roleId"
                LEFT JOIN "teamManagement" tm ON u.id = tm."teamLeaderId" AND tm."isDeleted" = 0
                WHERE ur."isTeamLeader" = true 
                    AND u."isDeleted" = 0
                ORDER BY u."fullName" ASC
            """
            team_leaders = execute_query(leaders_query, many=True)
            
            # Add validation message for team leaders who already have teams
            for leader in team_leaders:
                if leader.get('hasTeam'):
                    leader['validationMessage'] = f"Team '{leader.get('teamName')}' already exists for this team leader"
                else:
                    leader['validationMessage'] = None
            
            return success_response(
                data=team_leaders,
                message="Team leaders retrieved successfully"
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching team leaders: {str(e)}",
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

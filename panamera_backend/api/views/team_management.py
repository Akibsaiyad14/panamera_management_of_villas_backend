from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response, log_activity_raw
from rest_framework import status as http_status
from django.db import transaction
import json


class TeamManagementView(APIView):
    """
    Complete CRUD operations for team management.
    GET - List all teams or get single team
    POST - Create new team
    PUT - Update existing team
    DELETE - Soft delete team
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        GET /api/teams/ - List all active teams with pagination, filtering, and sorting
        Query params: 
            - page, pageSize: Pagination
            - sortBy, sortOrder: Sorting (teamName, createdAt, teamLeaderId)
            - search: Filter by team name
            - leaderId: Filter by team leader ID
        Response includes member details based on memberIds for each team.
        """
        try:
            # List all teams with pagination, filtering, and sorting
            search = request.GET.get('search', '').strip()
            leader_id = request.GET.get('leaderId')
            sort_by = request.GET.get('sortBy', 'createdAt')
            sort_order = request.GET.get('sortOrder', 'DESC').upper()
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('pageSize', 10))
            
            # Validate sort order
            if sort_order not in ['ASC', 'DESC']:
                sort_order = 'DESC'
            
            # Validate sort by column
            allowed_sort_columns = ['createdAt', 'teamName', 'teamLeaderId']
            if sort_by not in allowed_sort_columns:
                sort_by = 'createdAt'
            
            # Build WHERE clause
            where_clauses = ['tm."isDeleted" = 0']
            params = []
            
            if search:
                where_clauses.append(
                    '(tm."teamName" ILIKE %s OR leader."fullName" ILIKE %s)'
                )
                params.extend([f'%{search}%', f'%{search}%'])

            
            if leader_id:
                where_clauses.append('tm."teamLeaderId" = %s')
                params.append(leader_id)
            
            where_sql = ' AND '.join(where_clauses)
            
            # Get total count for pagination
            count_query = f"""
                SELECT COUNT(*) as total
                FROM "teamManagement" tm
                LEFT JOIN "user" leader ON tm."teamLeaderId" = leader.id
                WHERE {where_sql}
            """

            total_count = execute_query(count_query, params, many=False)[0]['total']
            total_pages = (total_count + page_size - 1) // page_size
            
            # Calculate offset
            offset = (page - 1) * page_size
            
            # Get paginated teams
            teams_query = f"""
                SELECT 
                    tm.id,
                    tm."teamName",
                    tm."teamLeaderId",
                    tm."memberIds",
                    tm."description",
                    tm."createdAt",
                    tm."updatedAt",
                    leader."fullName" as "teamLeaderName",
                    leader."employeeId" as "teamLeaderEmpId"
                FROM "teamManagement" tm
                LEFT JOIN "user" leader ON tm."teamLeaderId" = leader.id
                WHERE {where_sql}
                ORDER BY tm."{sort_by}" {sort_order}
                LIMIT %s OFFSET %s
            """
            teams = execute_query(teams_query, params + [page_size, offset], many=True)
            
            # Parse memberIds and add member count for each team
            for team in teams:
                # Convert memberIds from JSONB string to list
                if team.get('memberIds'):
                    team['memberIds'] = team['memberIds'] if isinstance(team['memberIds'], list) else json.loads(team['memberIds'])
                    team['memberCount'] = len(team['memberIds'])
                else:
                    team['memberIds'] = []
                    team['memberCount'] = 0
                
                # Get member details based on memberIds
                if team['memberIds']:
                    member_ids = team['memberIds']
                    placeholders = ','.join(['%s'] * len(member_ids))
                    members_query = f"""
                        SELECT 
                            u.id,
                            u."fullName",
                            u."employeeId",
                            u."phoneNumber",
                            u."roleId",
                            ur."roleName"
                        FROM "user" u
                        LEFT JOIN "userrole" ur ON u."roleId" = ur."roleId"
                        WHERE u.id IN ({placeholders}) AND u."isDeleted" = 0
                        ORDER BY u."fullName"
                    """
                    team['members'] = execute_query(members_query, member_ids, many=True)
                else:
                    team['members'] = []
            
            return success_response(
                data={
                    'results': teams,
                    "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                    }
                },
                message="Teams retrieved successfully"
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching teams: {str(e)}",
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):
        """
        POST /api/teams/ - Create new team
        Body: {
            "teamName": "Team Alpha",
            "teamLeaderId": 123,
            "memberIds": [1, 2, 3],
            "description": "Optional description"
        }
        """
        try:
            team_name = request.data.get('teamName', '').strip()
            team_leader_id = request.data.get('teamLeaderId')
            member_ids = request.data.get('memberIds', [])
            description = request.data.get('description', '').strip()

            # Validation
            if not team_name:
                return error_response(
                    message="Team name is required",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            if not team_leader_id:
                return error_response(
                    message="Team leader is required",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            # Verify team leader exists and has appropriate role
            leader_query = """
                SELECT u.id, u."fullName", u."roleId"
                FROM "user" u
                LEFT JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE u.id = %s AND ur."isTeamLeader" = true AND u."isDeleted" = 0
            """
            leader = execute_query(leader_query, [team_leader_id], many=False)
            
            if not leader or not leader[0]:
                return error_response(
                    message="Invalid team leader or user does not have team leader role",
                    status_code=http_status.HTTP_404_NOT_FOUND
                )

            team_leader_name = leader[0]['fullName']

            # Check if team leader already has a team
            existing_team_query = """
                SELECT id, "teamName" FROM "teamManagement"
                WHERE "teamLeaderId" = %s AND "isDeleted" = 0
            """
            existing_team = execute_query(existing_team_query, [team_leader_id], many=False)
            
            if existing_team and existing_team[0]:
                return error_response(
                    message="Team already exists for this team leader",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            # Verify members exist if provided
            if member_ids and not isinstance(member_ids, list):
                return error_response(
                    message="memberIds must be an array",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            if member_ids:
                placeholders = ','.join(['%s'] * len(member_ids))
                members_query = f"""
                    SELECT id FROM "user"
                    WHERE id IN ({placeholders}) AND "isDeleted" = 0
                """
                members = execute_query(members_query, member_ids, many=True)
                
                if len(members) != len(member_ids):
                    found_ids = [m['id'] for m in members]
                    missing_ids = [mid for mid in member_ids if mid not in found_ids]
                    return error_response(
                        message=f"Some members not found: {missing_ids}",
                        status_code=http_status.HTTP_404_NOT_FOUND
                    )

            # Create team
            with transaction.atomic():
                insert_query = """
                    INSERT INTO "teamManagement" 
                        ("teamName", "teamLeaderId", "memberIds", "description", "isDeleted", "createdBy", "createdAt")
                    VALUES (%s, %s, %s, %s, 0, %s, NOW())
                    RETURNING id
                """
                member_ids_json = json.dumps(member_ids) if member_ids else '[]'
                result = execute_query(
                    insert_query,
                    [team_name, team_leader_id, member_ids_json, description, request.user.id],
                    many=False
                )
                
                team_id = result[0]['id']

                # Update teamLeaderId in user table for all members
                if member_ids:
                    placeholders = ','.join(['%s'] * len(member_ids))
                    update_user_query = f"""
                        UPDATE "user"
                        SET "teamLeaderId" = %s
                        WHERE id IN ({placeholders})
                    """
                    execute_query(update_user_query, [team_leader_id] + member_ids)

                # Log activity
                log_activity_raw(
                    request=request,
                    category="Team",
                    action="Create",
                    performer=request.user,
                    details={
                        'teamId': team_id,
                        'teamName': team_name,
                        'teamLeaderId': team_leader_id,
                        'memberCount': len(member_ids),
                        "teamLeaderName": team_leader_name
                    }
                )

                return success_response(
                    data={
                        'teamId': team_id,
                        'teamName': team_name
                    },
                    message="Team created successfully",
                    status_code=http_status.HTTP_201_CREATED
                )

        except Exception as e:
            return error_response(
                message=f"Error creating team: {str(e)}",
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def put(self, request, team_id):
        """
        PUT /api/teams/<id>/ - Update existing team
        Body: {
            "teamName": "Team Alpha Updated",
            "teamLeaderId": 123,
            "memberIds": [1, 2, 3, 4],
            "description": "Updated description"
        }
        """
        try:
            user = request.user
            print("Updating team:", user)
            # Check if team exists
            team_query = """
                SELECT id, "teamName" FROM "teamManagement"
                WHERE id = %s AND "isDeleted" = 0
            """
            team = execute_query(team_query, [team_id], many=False)
            
            if not team or not team[0]:
                return error_response(
                    message="Team not found",
                    status_code=http_status.HTTP_404_NOT_FOUND
                )

            team_name = request.data.get('teamName', '').strip()
            team_leader_id = request.data.get('teamLeaderId')
            member_ids = request.data.get('memberIds', [])
            description = request.data.get('description', '').strip()

            # Build update query dynamically
            update_fields = []
            params = []

            if team_name:
                update_fields.append('"teamName" = %s')
                params.append(team_name)

            if team_leader_id:
                # Verify team leader
                leader_query = """
                    SELECT u.id FROM "user" u
                    LEFT JOIN "userrole" ur ON u."roleId" = ur."roleId"
                    WHERE u.id = %s AND ur."isTeamLeader" = true AND u."isDeleted" = 0
                """
                leader = execute_query(leader_query, [team_leader_id], many=False)
                
                
                if not leader or not leader[0]:
                    return error_response(
                        message="Invalid team leader",
                        status_code=http_status.HTTP_404_NOT_FOUND
                    )
                
                update_fields.append('"teamLeaderId" = %s')
                params.append(team_leader_id)

            if member_ids is not None:
                if not isinstance(member_ids, list):
                    return error_response(
                        message="memberIds must be an array",
                        status_code=http_status.HTTP_400_BAD_REQUEST
                    )
                
                # Verify members if provided
                if member_ids:
                    placeholders = ','.join(['%s'] * len(member_ids))
                    members_query = f"""
                        SELECT id FROM "user"
                        WHERE id IN ({placeholders}) AND "isDeleted" = 0
                    """
                    members = execute_query(members_query, member_ids, many=True)
                    
                    if len(members) != len(member_ids):
                        found_ids = [m['id'] for m in members]
                        missing_ids = [mid for mid in member_ids if mid not in found_ids]
                        return error_response(
                            message=f"Some members not found: {missing_ids}",
                            status_code=http_status.HTTP_404_NOT_FOUND
                        )
                
                update_fields.append('"memberIds" = %s')
                params.append(json.dumps(member_ids))

            if description is not None:
                update_fields.append('"description" = %s')
                params.append(description)

            if not update_fields:
                return error_response(
                    message="No fields to update",
                    status_code=http_status.HTTP_400_BAD_REQUEST
                )

            # Add updated metadata
            update_fields.append('"updatedBy" = %s')
            update_fields.append('"updatedAt" = NOW()')
            params.extend([request.user.id, team_id])

            # Update team
            with transaction.atomic():
                # Get old team data BEFORE updating (for teamLeaderId management)
                old_team_data = None
                if member_ids is not None:
                    old_team_query = """
                        SELECT "memberIds", "teamLeaderId" FROM "teamManagement"
                        WHERE id = %s
                    """
                    old_team_data = execute_query(old_team_query, [team_id], many=False)[0]
                
                # Now update the team
                update_query = f"""
                    UPDATE "teamManagement"
                    SET {', '.join(update_fields)}
                    WHERE id = %s
                """
                execute_query(update_query, params)

                # Update teamLeaderId in user table if members changed
                if member_ids is not None and old_team_data:
                    old_member_ids = old_team_data['memberIds'] if isinstance(old_team_data['memberIds'], list) else json.loads(old_team_data['memberIds']) if old_team_data['memberIds'] else []
                    old_team_leader_id = old_team_data['teamLeaderId']
                    
                    # Remove teamLeaderId from old members who are not in new list
                    removed_members = [m for m in old_member_ids if m not in member_ids]
                    if removed_members:
                        placeholders = ','.join(['%s'] * len(removed_members))
                        clear_query = f"""
                            UPDATE "user"
                            SET "teamLeaderId" = NULL
                            WHERE id IN ({placeholders})
                        """
                        execute_query(clear_query, removed_members)
                    
                    # Update teamLeaderId for new members
                    if member_ids:
                        new_team_leader_id = team_leader_id if team_leader_id else old_team_leader_id
                        placeholders = ','.join(['%s'] * len(member_ids))
                        update_user_query = f"""
                            UPDATE "user"
                            SET "teamLeaderId" = %s
                            WHERE id IN ({placeholders})
                        """
                        execute_query(update_user_query, [new_team_leader_id] + member_ids)

                # Log activity
                log_activity_raw(
                    request=request,
                    category="Team",
                    action="Update",
                    performer=request.user,
                    details={
                        'teamName': team_name,
                        'updates': request.data
                    }
                )

                return success_response(
                    message="Team updated successfully"
                )

        except Exception as e:
            return error_response(
                message=f"Error updating team: {str(e)}",
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request):
        """
        DELETE /api/teams/ - Soft delete multiple teams
        Body: {"teamIds": [1, 2, 3]} for bulk delete
        """
        try:
            # Handle bulk deletion from request body
            team_ids = request.data.get("teamIds", [])
            
            
            placeholders = ','.join(['%s'] * len(team_ids))

            # Fetch team details BEFORE deleting for accurate logging
            query_details = f"""
                SELECT id, "teamName" 
                FROM "teamManagement" 
                WHERE id IN ({placeholders}) AND "isDeleted" = 0
            """
            teams_to_delete = execute_query(query_details, team_ids, many=True)
            
            if not teams_to_delete:
                return error_response(
                    message="No valid teams found to delete",
                    status_code=http_status.HTTP_404_NOT_FOUND
                )

            # Perform the soft delete operation
            with transaction.atomic():
                # Get all member IDs from teams being deleted
                members_query = f"""
                    SELECT "memberIds" FROM "teamManagement"
                    WHERE id IN ({placeholders}) AND "isDeleted" = 0
                """
                teams_members = execute_query(members_query, team_ids, many=True)
                
                all_members_to_clear = set()
                for team_data in teams_members:
                    if team_data.get('memberIds'):
                        member_ids_list = team_data['memberIds'] if isinstance(team_data['memberIds'], list) else json.loads(team_data['memberIds'])
                        all_members_to_clear.update(member_ids_list)
                
                # Clear teamLeaderId for all members
                if all_members_to_clear:
                    clear_placeholders = ','.join(['%s'] * len(all_members_to_clear))
                    clear_query = f"""
                        UPDATE "user"
                        SET "teamLeaderId" = NULL
                        WHERE id IN ({clear_placeholders})
                    """
                    execute_query(clear_query, list(all_members_to_clear))
                
                delete_query = f"""
                    UPDATE "teamManagement"
                    SET "isDeleted" = 1,
                        "deletedBy" = %s,
                        "deletedAt" = NOW()
                    WHERE id IN ({placeholders}) AND "isDeleted" = 0
                """
                execute_query(delete_query, [request.user.id] + team_ids)

                # Log each deletion
                for team in teams_to_delete:
                    log_activity_raw(
                        request=request,
                        category="Team",
                        action="Delete",
                        performer=request.user,
                        details={
                            'teamId': team['id'],
                            'teamName': team['teamName']
                        }
                    )

                # Return appropriate message
                if len(teams_to_delete) == 1:
                    return success_response(
                        message=f"Team '{teams_to_delete[0]['teamName']}' deleted successfully"
                    )
                else:
                    return success_response(
                        message=f"{len(teams_to_delete)} teams deleted successfully"
                    )

        except Exception as e:
            return error_response(
                message=f"Error deleting teams: {str(e)}",
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AvailableMembersView(APIView):
    """
    Get list of employees who are not assigned to any team.
    GET /api/available-members/ - List available members with pagination
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        GET /api/available-members/ - List available team members
        Query params: search, page, pageSize, sortBy, sortOrder
        """
        try:
            search = request.GET.get('search', '').strip()
            sort_by = request.GET.get('sortBy', 'fullName')
            sort_order = request.GET.get('sortOrder', 'ASC').upper()
            
            # Validate sort order
            if sort_order not in ['ASC', 'DESC']:
                sort_order = 'ASC'
            
            # Validate sort by column
            allowed_sort_columns = ['fullName', 'employeeId']
            if sort_by not in allowed_sort_columns:
                sort_by = 'fullName'
            
            # Get all assigned member IDs from all teams
            teams_query = """
                SELECT "memberIds" FROM "teamManagement"
                WHERE "isDeleted" = 0
            """
            teams = execute_query(teams_query, [], many=True)
            
            all_assigned_members = set()
            for team in teams:
                if team.get('memberIds'):
                    member_ids = team['memberIds'] if isinstance(team['memberIds'], list) else json.loads(team['memberIds'])
                    all_assigned_members.update(member_ids)
            
            # Build WHERE clause
            # Get the maximum roleOrderId for team leaders to filter only users below them
            team_leader_role_query = """
                SELECT MAX("roleOrderId") as max_team_leader_order
                FROM "userrole"
                WHERE "isTeamLeader" = true
            """
            team_leader_role_result = execute_query(team_leader_role_query, [], many=False)
            # print(team_leader_role_result)
            max_team_leader_order = team_leader_role_result[0].get('max_team_leader_order') if team_leader_role_result and team_leader_role_result[0] else 999
            # print("Max Team Leader Order:", max_team_leader_order)
            
            where_clauses = [
                'ur."isTeamLeader" = false', 
                'u."isDeleted" = 0',
                'ur."roleOrderId" = %s'
            ]
            params = [max_team_leader_order]
            
            if all_assigned_members:
                placeholders = ','.join(['%s'] * len(all_assigned_members))
                where_clauses.append(f'u.id NOT IN ({placeholders})')
                params.extend(list(all_assigned_members))
            
            if search:
                where_clauses.append('(u."fullName" ILIKE %s OR u."employeeId" ILIKE %s)')
                params.extend([f'%{search}%', f'%{search}%'])
            
            where_sql = ' AND '.join(where_clauses)
            

            # Get paginated available members
            members_query = f"""
                SELECT 
                    u.id,
                    u."fullName",
                    u."employeeId",
                    u."phoneNumber",
                    u."roleId",
                    ur."roleOrderId",
                    ur."roleName"
                FROM "user" u
                LEFT JOIN "userrole" ur ON u."roleId" = ur."roleId"
                WHERE {where_sql}
                ORDER BY u."{sort_by}" {sort_order}
            """
            available_members = execute_query(members_query, params, many=True)
            
            return success_response(
                data=
                    available_members
                ,
                message="Available members retrieved successfully"
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching available members: {str(e)}",
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

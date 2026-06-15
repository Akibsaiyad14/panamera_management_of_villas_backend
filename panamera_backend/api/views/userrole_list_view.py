from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, log_activity_raw
import json

class UserRoleList(APIView):
    permission_classes = [IsAuthenticated]


    def get(self, request):
        try:
            query = """
                SELECT
                    "roleId",
                    "roleName",
                    "reportingToRoleId",
                    "roleOrderId",
                    "functionalityKey",
                    "groupNumber",
                    "isTeamLeader"
                FROM "userrole"
                WHERE "isDeleted" = 0
                ORDER BY "roleOrderId" ASC
            """
            roles = execute_query(query, many=True)

            # --- FIX STARTS HERE ---
            # Manually parse the 'functionalityKey' string back into a list
            if roles:
                for role in roles:
                    # Check if the key exists and is a string before trying to parse
                    if role.get('functionalityKey') and isinstance(role['functionalityKey'], str):
                        try:
                            role['functionalityKey'] = json.loads(role['functionalityKey'])
                        except json.JSONDecodeError:
                            # Handle cases where the string is not valid JSON, maybe set to empty list
                            role['functionalityKey'] = []
            # --- FIX ENDS HERE ---

            # If you're fetching a single role, the logic is slightly different
            # if isinstance(roles, dict): # Check if it's a single role object
            #     if roles.get('functionalityKey') and isinstance(roles['functionalityKey'], str):
            #         roles['functionalityKey'] = json.loads(roles['functionalityKey'])

            return success_response(data=roles, message="Role list fetched successfully")
        except Exception as e:
            return error_response(message=f"Error fetching role list: {str(e)}", status_code=500)


    def post(self, request):
        # user_id = request.user.id
        # role_info = self.get_user_role(user_id)
        # if not role_info or role_info['roleId'] != 1:
        #     return error_response("Only Admin can add roles", status.HTTP_403_FORBIDDEN)

        role_name = request.data.get('roleName')
        role_order_id = request.data.get('roleOrderId')
        reporting_to_roleid = request.data.get('reportingToRoleId')
        group_number = request.data.get('groupNumber')
        is_team_leader = request.data.get('isTeamLeader', False)

        # Get the new optional "functionalityKey" field which expects a list (JSON array)
        functionality_key = request.data.get('functionalityKey') # This can be a list or None

        if not all([role_name, role_order_id, reporting_to_roleid, group_number]):
            return error_response(
                message="roleName, roleOrderId, reportingToRoleId, and groupNumber are required",
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Optional but recommended: Validate that if functionalityKey is provided, it is a list.
        if functionality_key is not None and not isinstance(functionality_key, list):
            return error_response(
                message="functionalityKey must be a list (JSON array).",
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Serialize the Python list to a JSON string for the database driver.
        # If functionality_key is None, it will be stored as SQL NULL.
        functionality_key_json = json.dumps(functionality_key) if functionality_key is not None else None

        insert_query = """
            INSERT INTO "userrole" ("roleName", "reportingToRoleId", "roleOrderId", "functionalityKey", "groupNumber", "isTeamLeader")
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        params = [role_name, reporting_to_roleid, role_order_id, functionality_key_json, group_number, is_team_leader]
        # print(insert_query, params)
        execute_query(insert_query, params, fetch=True)

            # --- THIS IS THE CRUCIAL FIX ---
            # Check if the query returned a valid list of results.
            # If it returned True/False or an empty list, something went wrong.
        # if not isinstance(new_role_result, list) or not new_role_result:
        #         # Log this error for debugging
        #         # logger.error("Failed to create user role: execute_query did not return the new ID.")
        #     return error_response(
        #             message="Failed to create the user role and retrieve its ID.",
        #             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        #         )

        #     # Now it's safe to access the result
        # new_role_id = new_role_result[0]['roleId']

            # --- LOG THE "ADD" ACTIVITY ---
        log_activity_raw(
                request=request,
                category='UserAccess',
                action='Add',
                performer=request.user,
                details={
                    # 'newRoleId': new_role_id,
                    'newRoleName': request.data.get('roleName')
                }
            )


        return success_response(message="Userrole added successfully", status_code=status.HTTP_201_CREATED)

    def put(self, request):
        # user_id = request.user.id
        # role_info = self.get_user_role(user_id)
        # if not role_info or role_info['roleId'] != 1:
        #     return error_response("Only Admin can update roles", status.HTTP_403_FORBIDDEN)

        role_id = request.data.get('roleId')
        if not role_id:
            return error_response(message="roleId is required for an update", status_code=status.HTTP_400_BAD_REQUEST)

        # --- Dynamic Update Logic for Partial Updates ---
        update_fields = []
        params = []

        if 'roleName' in request.data:
            update_fields.append('"roleName" = %s')
            params.append(request.data.get('roleName'))

        if 'roleOrderId' in request.data:
            update_fields.append('"roleOrderId" = %s')
            params.append(request.data.get('roleOrderId'))

        if 'reportingToRoleId' in request.data:
            update_fields.append('"reportingToRoleId" = %s')
            params.append(request.data.get('reportingToRoleId'))

        if 'groupNumber' in request.data:
            update_fields.append('"groupNumber" = %s')
            params.append(request.data.get('groupNumber'))
        if 'isTeamLeader' in request.data:
            update_fields.append('"isTeamLeader" = %s')
            params.append(request.data.get('isTeamLeader'))

        # Handle the new functionalityKey field
        if 'functionalityKey' in request.data:
            functionality_key = request.data.get('functionalityKey')

            # Validate the provided value
            if functionality_key is not None and not isinstance(functionality_key, list):
                return error_response(
                    message="functionalityKey must be a list (JSON array).",
                    status_code=status.HTTP_400_BAD_REQUEST
                 )

            # Serialize to JSON string or None for SQL NULL
            functionality_key_json = json.dumps(functionality_key) if functionality_key is not None else None
            update_fields.append('"functionalityKey" = %s')
            params.append(functionality_key_json)

        if not update_fields:
            return error_response(message="No fields provided to update.", status_code=status.HTTP_400_BAD_REQUEST)

        # Construct the SET clause from the fields that were provided in the request
        set_clause = ", ".join(update_fields)

        update_query = f"""
            UPDATE "userrole"
            SET {set_clause}
            WHERE "roleId" = %s AND "isDeleted" = 0
        """

        # Add the roleId for the WHERE clause to the end of the parameters list
        params.append(role_id)

        execute_query(update_query, params, fetch=False)


        log_activity_raw(
                request=request,
                category='UserAccess',
                action='Update',
                performer=request.user,
                target_employee_name= None,  # Optional, can be None
                details={
                    'roleName': request.data.get('roleName', 'N/A'),
                    'updatedRoleId': role_id,
                    'performerId': str(request.user.id) if request.user and hasattr(request.user, 'id') else 'N/A',
                    'updatedFields': list(request.data.keys()) # Log which fields were updated
                }
            )

        return success_response(message="Userrole updated successfully", status_code=status.HTTP_200_OK)



    def delete(self, request):


        # It will Delete a List of roleIds
        role_id = request.data.get('roleIds')
        if not role_id or not isinstance(role_id, list) or not all(isinstance(id, int) for id in role_id):
            return error_response(message="roleId must be a non-empty list of integers", status_code=status.HTTP_400_BAD_REQUEST)
        placeholders = ','.join(['%s'] * len(role_id))
        delete_query = f"""
            UPDATE "userrole"
            SET "isDeleted" = 1
            WHERE "roleId" IN ({placeholders}) AND "isDeleted" = 0
        """
        deleted_roles_result = execute_query(delete_query, role_id, fetch=True)

        if not deleted_roles_result:
            return error_response(message="No matching roles found or they were already deleted.", status_code=status.HTTP_404_NOT_FOUND)

        actually_deleted_ids = [role['roleId'] for role in deleted_roles_result]

            # --- LOG THE "DELETE" ACTIVITY ---
        log_activity_raw(
                request=request,
                category='UserAccess',
                action='Delete',
                performer=request.user,
                target_employee_name= None, # This is a bulk action on roles
                details={
                    'deletedRoleIds': actually_deleted_ids,
                    'count': len(actually_deleted_ids)
                }
            )
        return success_response(message=ROLE_DELETED_SUCCESSFULLY)

from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response, log_activity_raw  # adjust import as per your structure
from api.messages import USER_IDS_MUST_BE_NON_EMPTY_LIST


class ShiftMapView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Get a user's shift schedule for the week
        Query param: userId
        """
        try:
            user_id = request.query_params.get("userId")
            
            if not user_id:
                return error_response(message="userId is required", status_code=400)

            # Get day-wise shift mapping with shift details
            query = """
                SELECT 
                    m."dayOfWeek",
                    m."shiftId",
                    s."shiftName",
                    s."startTime",
                    s."endTime"
                FROM "userShiftDayMapping" m
                INNER JOIN shifts s ON m."shiftId" = s."shiftId"
                WHERE m."userId" = %s AND s."isDeleted" = 0
                ORDER BY 
                    CASE m."dayOfWeek"
                        WHEN 'Monday' THEN 1
                        WHEN 'Tuesday' THEN 2
                        WHEN 'Wednesday' THEN 3
                        WHEN 'Thursday' THEN 4
                        WHEN 'Friday' THEN 5
                        WHEN 'Saturday' THEN 6
                        WHEN 'Sunday' THEN 7
                    END
            """
            
            schedule = execute_query(query, [user_id], many=True)
            
            # Format response as a week schedule
            week_schedule = {
                'Monday': None,
                'Tuesday': None,
                'Wednesday': None,
                'Thursday': None,
                'Friday': None,
                'Saturday': None,
                'Sunday': None
            }
            
            for entry in schedule:
                week_schedule[entry['dayOfWeek']] = {
                    'shiftId': entry['shiftId'],
                    'shiftName': entry['shiftName'],
                    'startTime': str(entry['startTime']) if entry['startTime'] else None,
                    'endTime': str(entry['endTime']) if entry['endTime'] else None,
                    'breakDuration': entry['breakDuration']
                }
            
            return success_response(
                data=week_schedule,
                message="User shift schedule fetched successfully"
            )

        except Exception as e:
            return error_response(message=f"Error fetching shift schedule: {str(e)}", status_code=500)

    def post(self, request):
        try:
            # Check which mode based on request parameters
            week_schedule = request.data.get("weekSchedule")
            user_ids = request.data.get("userIds")
            user_id = request.data.get("userId")
            
            if week_schedule is not None:
                if user_ids is not None and isinstance(user_ids, list):
                    # MODE 1: Bulk Weekly Assignment - Same weekly schedule to multiple users
                    return self._handle_bulk_weekly_assignment(request)
                elif user_id is not None:
                    # MODE 2: Single User Weekly Scheduling - Different shifts per day for one user
                    return self._handle_weekly_schedule(request)
                else:
                    return error_response(message="Either userId or userIds must be provided with weekSchedule", status_code=400)
            else:
                # MODE 3: Bulk Simple Assignment - Same shift to multiple users for all days
                return self._handle_bulk_assignment(request)

        except Exception as e:
            return error_response(message=f"Error mapping shift to users: {str(e)}", status_code=500)

    def _handle_bulk_weekly_assignment(self, request):
        """
        Handle bulk weekly shift scheduling for multiple users
        Assigns the SAME weekly schedule to MULTIPLE users
        Request: {
            "userIds": [101, 102, 103],
            "weekSchedule": {
                "Monday": 1,
                "Tuesday": 2,
                "Wednesday": 1,
                "Thursday": 2,
                "Friday": 1,
                "Saturday": null,
                "Sunday": null
            }
        }
        """
        user_ids = request.data.get("userIds")
        week_schedule = request.data.get("weekSchedule")
        
        # Validate userIds
        if not isinstance(user_ids, list) or not user_ids:
            return error_response(message="userIds must be a non-empty array", status_code=400)
        
        # Validate weekSchedule
        if not isinstance(week_schedule, dict):
            return error_response(message="weekSchedule must be an object/dictionary", status_code=400)
        
        valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        
        # Validate all days are present
        missing_days = [day for day in valid_days if day not in week_schedule]
        if missing_days:
            return error_response(message=f"Missing days in weekSchedule: {missing_days}", status_code=400)
        
        # Check if users exist
        placeholders = ','.join(['%s'] * len(user_ids))
        user_check = execute_query(
            f'SELECT COUNT(*) FROM "user" WHERE id IN ({placeholders}) AND "isDeleted" = 0',
            user_ids,
            many=False
        )
        if user_check[0]['count'] != len(user_ids):
            return error_response(message="One or more invalid userIds provided", status_code=404)
        
        # Validate all shift IDs exist
        shift_ids = [week_schedule[day] for day in valid_days if week_schedule[day] is not None]
        if shift_ids:
            unique_shift_ids = list(set(shift_ids))
            shift_placeholders = ','.join(['%s'] * len(unique_shift_ids))
            shift_check = execute_query(
                f'SELECT "shiftId" FROM shifts WHERE "shiftId" IN ({shift_placeholders}) AND "isDeleted" = 0',
                unique_shift_ids,
                many=True
            )
            valid_shift_ids = [s['shiftId'] for s in shift_check]
            invalid_shifts = [sid for sid in unique_shift_ids if sid not in valid_shift_ids]
            if invalid_shifts:
                return error_response(message=f"Invalid shiftIds: {invalid_shifts}", status_code=400)
        
        # Process each user
        first_shift_id = None
        for day in valid_days:
            if week_schedule[day] is not None:
                first_shift_id = week_schedule[day]
                break
        
        for user_id in user_ids:
            # Delete existing mappings for this user
            execute_query(
                'DELETE FROM "userShiftDayMapping" WHERE "userId" = %s',
                [user_id],
                many=False
            )
            
            # Insert new mappings
            for day in valid_days:
                shift_id = week_schedule[day]
                if shift_id is not None:
                    insert_query = """
                        INSERT INTO "userShiftDayMapping" ("userId", "shiftId", "dayOfWeek", "createdAt", "updatedAt")
                        VALUES (%s, %s, %s, NOW(), NOW())
                    """
                    execute_query(insert_query, [user_id, shift_id, day], many=False)
        
        # Update user table for backward compatibility
        if first_shift_id is not None:
            update_query = f"""
                UPDATE "user" SET "shiftId" = %s, "shiftUpdateTime" = NOW()
                WHERE id IN ({placeholders}) AND "isDeleted" = 0
            """
            execute_query(update_query, [first_shift_id] + user_ids, many=False)
        else:
            # No shifts assigned, set to NULL
            update_query = f"""
                UPDATE "user" SET "shiftId" = NULL, "shiftUpdateTime" = NOW()
                WHERE id IN ({placeholders}) AND "isDeleted" = 0
            """
            execute_query(update_query, user_ids, many=False)
        
        # Log as single weekly schedule when payload contains exactly one user.
        if len(user_ids) == 1:
            user_info = execute_query(
                'SELECT "fullName" FROM "user" WHERE id = %s',
                [user_ids[0]],
                many=False
            )
            target_employee_name = user_info[0]['fullName'] if user_info else 'N/A'
            assigned_days = sum(1 for day in valid_days if week_schedule.get(day) is not None)

            log_activity_raw(
                request=request,
                category='ShiftMapping',
                action='SetWeeklySchedule',
                performer=request.user,
                target_employee_name=target_employee_name,
                target_shift_id=None,
                details={'userId': user_ids[0], 'daysAssigned': assigned_days, 'weekSchedule': week_schedule}
            )
        else:
            log_activity_raw(
                request=request,
                category='ShiftMapping',
                action='BulkSetWeeklySchedule',
                performer=request.user,
                target_employee_name=None,
                target_shift_id=None,
                details={'userIds': user_ids, 'count': len(user_ids), 'weekSchedule': week_schedule}
            )
        
        return success_response(
            data={
                "userIds": user_ids,
                "usersUpdated": len(user_ids),
                "weekSchedule": week_schedule
            },
            message=f"Shift schedule updated successfully"
        )

    def _handle_weekly_schedule(self, request):
        """
        Handle weekly shift scheduling for a single user
        Request: {
            "userId": 123,
            "weekSchedule": {
                "Monday": 1,
                "Tuesday": 2,
                "Wednesday": 1,
                "Thursday": 2,
                "Friday": 1,
                "Saturday": null,
                "Sunday": null
            }
        }
        """
        user_id = request.data.get("userId")
        week_schedule = request.data.get("weekSchedule")
        
        # Validate userId
        if not user_id:
            return error_response(message="userId is required for weekly scheduling", status_code=400)
        
        # Validate weekSchedule
        if not isinstance(week_schedule, dict):
            return error_response(message="weekSchedule must be an object/dictionary", status_code=400)
        
        valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        
        # Validate all days are present
        missing_days = [day for day in valid_days if day not in week_schedule]
        if missing_days:
            return error_response(message=f"Missing days in weekSchedule: {missing_days}", status_code=400)
        
        # Check if user exists
        user_check = execute_query(
            'SELECT COUNT(*) FROM "user" WHERE id = %s AND "isDeleted" = 0',
            [user_id],
            many=False
        )
        if user_check[0]['count'] == 0:
            return error_response(message="Invalid userId provided", status_code=404)
        
        # Validate all shift IDs exist
        shift_ids = [week_schedule[day] for day in valid_days if week_schedule[day] is not None]
        if shift_ids:
            unique_shift_ids = list(set(shift_ids))
            placeholders = ','.join(['%s'] * len(unique_shift_ids))
            shift_check = execute_query(
                f'SELECT "shiftId" FROM shifts WHERE "shiftId" IN ({placeholders}) AND "isDeleted" = 0',
                unique_shift_ids,
                many=True
            )
            valid_shift_ids = [s['shiftId'] for s in shift_check]
            invalid_shifts = [sid for sid in unique_shift_ids if sid not in valid_shift_ids]
            if invalid_shifts:
                return error_response(message=f"Invalid shiftIds: {invalid_shifts}", status_code=400)
        
        # Delete existing mappings for this user
        execute_query(
            'DELETE FROM "userShiftDayMapping" WHERE "userId" = %s',
            [user_id],
            many=False
        )
        
        # Insert new mappings
        inserted_count = 0
        first_shift_id = None
        
        for day in valid_days:
            shift_id = week_schedule[day]
            if shift_id is not None:
                if first_shift_id is None:
                    first_shift_id = shift_id
                
                insert_query = """
                    INSERT INTO "userShiftDayMapping" ("userId", "shiftId", "dayOfWeek", "createdAt", "updatedAt")
                    VALUES (%s, %s, %s, NOW(), NOW())
                """
                execute_query(insert_query, [user_id, shift_id, day], many=False)
                inserted_count += 1
        
        # Update user table with first shift for backward compatibility
        if first_shift_id is not None:
            execute_query(
                'UPDATE "user" SET "shiftId" = %s, "shiftUpdateTime" = NOW() WHERE id = %s',
                [first_shift_id, user_id],
                many=False
            )
        else:
            # No shifts assigned, set to NULL
            execute_query(
                'UPDATE "user" SET "shiftId" = NULL, "shiftUpdateTime" = NOW() WHERE id = %s',
                [user_id],
                many=False
            )
        
        # Log activity
        user_info = execute_query(
            'SELECT "employeeId", "fullName" FROM "user" WHERE id = %s',
            [user_id],
            many=False
        )
        target_employee_name = user_info[0]['fullName'] if user_info else 'N/A'
        
        log_activity_raw(
            request=request,
            category='ShiftMapping',
            action='SetWeeklySchedule',
            performer=request.user,
            target_employee_name=target_employee_name,
            target_shift_id=None,
            details={'userId': user_id, 'daysAssigned': inserted_count, 'weekSchedule': week_schedule}
        )
        
        return success_response(
            data={
                "userId": user_id,
                "daysAssigned": inserted_count,
                "weekSchedule": week_schedule
            },
            message="Weekly shift schedule set successfully"
        )

    def _handle_bulk_assignment(self, request):
        """
        Handle bulk shift assignment to multiple users
        Request: {
            "shiftId": 1,  // or null to unassign
            "userIds": [101, 102, 103]
        }
        """
        try:
            shift_id = request.data.get("shiftId")
            user_ids = request.data.get("userIds")

            # Validate user_ids
            if not isinstance(user_ids, list) or not user_ids:
                return error_response(message=USER_IDS_MUST_BE_NON_EMPTY_LIST, status_code=400)

            # Validate shift exists (if shiftId is provided)
            if shift_id is not None:
                shift_check_query = 'SELECT COUNT(*) FROM shifts WHERE "shiftId" = %s AND "isDeleted" = 0'
                shift_exists = execute_query(shift_check_query, [shift_id], many=False)
                if shift_exists[0]['count'] == 0:
                    return error_response(message="Invalid shiftId provided", status_code=400)

            # Perform the database write
            placeholders = ','.join(['%s'] * len(user_ids))
            
            if shift_id is not None:
                # Assign shift to all days for these users
                valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
                
                for user_id in user_ids:
                    # Delete existing mappings
                    execute_query(
                        'DELETE FROM "userShiftDayMapping" WHERE "userId" = %s',
                        [user_id],
                        many=False
                    )
                    
                    # Insert shift for all days
                    for day in valid_days:
                        insert_query = """
                            INSERT INTO "userShiftDayMapping" ("userId", "shiftId", "dayOfWeek", "createdAt", "updatedAt")
                            VALUES (%s, %s, %s, NOW(), NOW())
                        """
                        execute_query(insert_query, [user_id, shift_id, day], many=False)
                
                # Update user table for backward compatibility
                update_query = f"""
                    UPDATE "user" SET "shiftId" = %s, "shiftUpdateTime" = NOW()
                    WHERE id IN ({placeholders}) AND "isDeleted" = 0 RETURNING id
                """
                params = [shift_id] + user_ids
            else:
                # Unassign shift - delete all day mappings
                for user_id in user_ids:
                    execute_query(
                        'DELETE FROM "userShiftDayMapping" WHERE "userId" = %s',
                        [user_id],
                        many=False
                    )
                
                update_query = f"""
                    UPDATE "user" SET "shiftId" = NULL, "shiftUpdateTime" = NOW()
                    WHERE id IN ({placeholders}) AND "isDeleted" = 0 RETURNING id
                """
                params = user_ids

            updated_users_result = execute_query(update_query, params, many=True)

            if not updated_users_result:
                return error_response(message="No users were updated. Check user IDs.", status_code=404)

            updated_ids = [user["id"] for user in updated_users_result]
            update_count = len(updated_ids)

            # --- LOG THE SHIFT MAPPING ACTIVITY (Corrected Logic) ---

            log_action = ''
            log_details = {}
            target_employee_name = None # This will hold the human-readable ID for singular actions

            if shift_id is not None:  # Assignment Logic
                if update_count == 1:
                    log_action = 'Assign'
                    # --- REQUIRED MODIFICATION: Fetch the employeeId for the log ---
                    user_info = execute_query('SELECT "employeeId", "fullName" FROM "user" WHERE id = %s', [updated_ids[0]], many=False)
                    target_employee_name = user_info[0]['fullName'] if user_info else 'N/A'
                    log_details = {'assignedUserId': updated_ids[0]}
                else:
                    log_action = 'BulkAssign'
                    log_details = {'assignedUserIds': updated_ids, 'count': update_count}
            else:  # Unassignment Logic
                if update_count == 1:
                    log_action = 'Unassign'
                    # --- REQUIRED MODIFICATION: Fetch the employeeId for the log ---
                    user_info = execute_query('SELECT "employeeId", "fullName" FROM "user" WHERE id = %s', [updated_ids[0]], many=False)
                    target_employee_name = user_info[0]['fullName'] if user_info else 'N/A'
                    log_details = {'unassignedUserId': updated_ids[0]}
                else:
                    log_action = 'BulkUnassign'
                    log_details = {'unassignedUserIds': updated_ids, 'count': update_count}

            # --- Call the logging function with the correct parameter names ---
            log_activity_raw(
                request=request,
                category='ShiftMapping',
                action=log_action,
                performer=request.user, # Correct parameter name
                target_employee_name=target_employee_name, # Correct parameter name
                target_shift_id=shift_id, # Pass the shift_id directly
                details=log_details
            )

            return success_response(
                data={"updatedUserIds": updated_ids},
                message="Shift mapping updated successfully."
            )

        except Exception as e:
            return error_response(message=f"Error mapping shift to users: {str(e)}", status_code=500)

from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
import json
from api.utils import success_response, error_response, execute_query, add_new_skills, generate_password, encode_password, log_activity_raw
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from django.contrib.auth import get_user_model
from django.utils.timezone import now

class EmployeeView(APIView):
    permission_classes = [IsAuthenticated]
    # Allowed fields for sorting
    allowed_sort_fields = [
        "employeeId", "fullName", "nationality", "gender",
        "dateOfBirth", "dateOfJoining", "department","roleName",
        "reportingToId", "employmentStatus", "skillSet", "reportingToName"
    ]

    def get(self, request, pk=None):
        try:
            user = request.user
            search = request.query_params.get("search", "").strip()
            sort_param = request.query_params.get("sort", "").strip()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            reporting_to_id = int(request.query_params.get("filterByReportingToId", 0))
            filter_by_role_id = int(request.query_params.get("filterByRoleId", 0))
            functionality_keys = request.query_params.get("functionalityKey", "").strip()  # New filter
            offset = (page - 1) * page_size

            where_clause = 'WHERE u."isDeleted" = 0 AND u."roleId" != 1'
            params = []

            if pk:
                single_employee = execute_query(
                    """
                    SELECT
                        u.id, u."userName", u."phoneNumber", u."employeeId", u."fullName",
                        u.nationality, u.gender, u."dateOfBirth", u."dateOfJoining", u."shiftId",
                        u."department", u."reportingToId",
                        u."employmentStatus", u."skillSet", u."shiftUpdateTime",
                        CASE
                            WHEN u."reportingToId" IS NULL THEN 'NA'
                            ELSE ru."fullName"
                        END AS "reportingToName",
                        u."roleId",
                        ur."roleName" AS "roleName"
                    FROM "user" u
                    LEFT JOIN "user" ru ON ru.id = u."reportingToId"
                    LEFT JOIN userrole ur ON u."roleId" = ur."roleId"
                    WHERE u.id = %s AND u."isDeleted" = 0
                    """,
                    [pk],
                    fetch=True,
                    many=False
                )

                if not single_employee:
                    return error_response(
                        message="Employee not found",
                        status_code=status.HTTP_404_NOT_FOUND
                    )

                # Fetch weekly shift schedule for this employee
                employee_data = single_employee[0] if isinstance(single_employee, list) else single_employee
                shift_schedule_query = """
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
                shift_schedule = execute_query(shift_schedule_query, [pk], many=True)
                
                # Format as week schedule
                week_schedule = {}
                for entry in shift_schedule:
                    week_schedule[entry['dayOfWeek']] = entry['shiftId']
                
                employee_data['weeklyShiftSchedule'] = week_schedule

                return success_response(
                    data=employee_data,
                    message=EMPLOYEE_FETCHED_SUCCESSFULLY
                )

            if search:
                page = 1
                where_clause += """ AND (
                    u."employeeId" ILIKE %s OR
                    u."fullName" ILIKE %s OR
                    u.nationality ILIKE %s OR
                    u.gender ILIKE %s OR
                    u."department" ILIKE %s OR
                    u."employmentStatus" ILIKE %s OR
                    u."skillSet" ILIKE %s OR
                    ur."roleName" ILIKE %s
                )"""
                params.extend([f"%{search}%"] * 8)

            if reporting_to_id and reporting_to_id > 0:
                where_clause += """ AND u."reportingToId" = %s"""
                params.append(reporting_to_id)

            if filter_by_role_id and filter_by_role_id > 0:
                where_clause += """ AND u."roleId" = %s"""
                params.append(filter_by_role_id)

            # Add functionalityKey filter
            if functionality_keys:
                key_list = [key.strip() for key in functionality_keys.split(",") if key.strip()]
                if key_list:
                    # Ensure all keys are present in functionalityKey
                    where_clause += """ AND EXISTS (
                        SELECT 1
                        FROM userrole ur2
                        WHERE ur2."roleId" = u."roleId"
                        AND ur2."functionalityKey" @> %s
                    )"""
                    params.append(json.dumps(key_list))

            # Sorting logic
            order_by = 'ORDER BY u.id DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    if sort_field == "reportingToName":
                        order_by = f'ORDER BY "reportingToName" {sort_direction}'
                    elif sort_field == "roleName":
                        order_by = f'ORDER BY LOWER(TRIM(ur."roleName")) {sort_direction}'
                    elif sort_field == "fullName":
                        order_by = f'ORDER BY LOWER(TRIM(u."fullName")) {sort_direction}'
                    else:
                        order_by = f'ORDER BY u."{sort_field}" {sort_direction}'

            if is_export:
                log_activity_raw(
                    request=request,
                    category='Other',
                    action='Employee',
                    performer=request.user,
                    details={
                        'filtersUsed': dict(request.query_params)
                    }
                )

            # Count total records for pagination meta
            count_query = f"""
                SELECT COUNT(*) AS total
                FROM "user" u
                LEFT JOIN "user" ru ON ru.id = u."reportingToId"
                LEFT JOIN userrole ur ON u."roleId" = ur."roleId"
                {where_clause}
            """
            total_result = execute_query(count_query, params, fetch=True)
            total_count = total_result[0]["total"]

            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            # 6. SMART PAGE RESET (The Fix)
            # If user asks for Page 5, but there are only 2 pages of results, reset to Page 1.
            # But if there ARE 5 pages of results, let them stay on Page 5.
            if total_pages > 0 and page > total_pages:
                page = 1
            if total_pages == 0:
                page = 1

            # 7. CALCULATE OFFSET NOW (After page is fixed)
            offset = (page - 1) * page_size

            # Main query
            query = f"""
                SELECT
                    u.id, u."userName", u."phoneNumber", u."employeeId", u."fullName",
                    u.nationality, u.gender, u."dateOfBirth", u."dateOfJoining", u."shiftId",
                    u."department", u."reportingToId",
                    u."employmentStatus", u."skillSet", u."shiftUpdateTime",
                    CASE
                        WHEN u."reportingToId" IS NULL THEN 'NA'
                        ELSE ru."fullName"
                    END AS "reportingToName",
                    u."roleId",
                    ur."roleName" AS "roleName"
                FROM "user" u
                LEFT JOIN "user" ru ON ru.id = u."reportingToId"
                LEFT JOIN userrole ur ON u."roleId" = ur."roleId"
                {where_clause}
                {order_by}
            """

            if not is_export:
                query += f"""
                LIMIT {page_size} OFFSET {offset}
                """

            employees = execute_query(query, params, many=True)

            # Fetch shift schedules for all employees in the result
            if employees:
                employee_ids = [emp['id'] for emp in employees]
                placeholders = ','.join(['%s'] * len(employee_ids))
                
                shift_schedule_query = f"""
                    SELECT 
                        m."userId",
                        m."dayOfWeek",
                        m."shiftId",
                        s."shiftName",
                        s."startTime",
                        s."endTime"
                    FROM "userShiftDayMapping" m
                    INNER JOIN shifts s ON m."shiftId" = s."shiftId"
                    WHERE m."userId" IN ({placeholders}) AND s."isDeleted" = 0
                    ORDER BY 
                        m."userId",
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
                shift_schedules = execute_query(shift_schedule_query, employee_ids, many=True)
                
                # Group shifts by userId
                shifts_by_user = {}
                for shift_entry in shift_schedules:
                    user_id = shift_entry['userId']
                    if user_id not in shifts_by_user:
                        shifts_by_user[user_id] = {}
                    
                    shifts_by_user[user_id][shift_entry['dayOfWeek']] = shift_entry['shiftId']
                
                # Add shift schedules to each employee
                for employee in employees:
                    employee['weeklyShiftSchedule'] = shifts_by_user.get(employee['id'], {})



            # total_pages = (total_count + page_size - 1) // page_size

            response_data = {
                "results": employees,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size
                }
            }

            return success_response(data=response_data, message=EMPLOYEE_FETCHED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=f"Error fetching employee list: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



    def post(self, request):
        # if not request.user.has_perm('auth.add_user'):
        #     return error_response(message="You don't have permission to add employees", status_code=status.HTTP_403_FORBIDDEN)
        try:
            data = request.data

            required_fields = [
                "userName", "phoneNumber", "employeeId", "fullName",
                "nationality", "gender", "dateOfBirth", "dateOfJoining",
                "employmentStatus", "department"
            ]
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                return error_response(message=f"Missing fields: {', '.join(missing_fields)}", status_code=status.HTTP_400_BAD_REQUEST)

            employee_id = data.get("employeeId")
            employee_name = data.get("fullName")

            check_query = 'SELECT COUNT(*) FROM "user" WHERE "employeeId" = %s'
            count_result = execute_query(check_query, [employee_id])
            if count_result[0]['count'] > 0:
                return error_response(message="Employee Id already exists", status_code=status.HTTP_400_BAD_REQUEST)

            user_role = data.get("roleId")
            if not user_role:
                return error_response(message=ROLE_ID_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)
            department = data.get("department")

            reporting_to_id = data.get("reportingToId")
            # if user_role not in [1, 2]:
            #     if not reporting_to_id:
            #         return error_response("Missing reportingToId for this role", status.HTTP_400_BAD_REQUEST)
            #     reporting_check_query = 'SELECT id FROM "user" WHERE id = %s AND "isDeleted" = 0'
            #     reporting_result = execute_query(reporting_check_query, [reporting_to_id])
            #     if not reporting_result:
            #         return error_response("Invalid reportingToId: no such user", status.HTTP_400_BAD_REQUEST)
            # else:
            #     reporting_to_id = None  # Admin and Supervisor roles do not require reportingToId


            generated_password = generate_password(3)
            encoded_password = encode_password(generated_password)

            skill_set = data.get("skillSet")

            if skill_set:
                try:
                    skill_set = add_new_skills(skill_set, execute_query)
                except ValueError as ve:
                    return error_response(str(ve), status.HTTP_400_BAD_REQUEST)
                skill_set_str = ', '.join(skill_set)
            else:
                skill_set_str = None

            # Now skill_set is a clean list you can insert as JSON, or convert back to string
            # If your column is text — convert to comma-separated string
            # skill_set_str = ', '.join(skill_set)

            insert_query = """
                INSERT INTO "user"
                ("userName", "phoneNumber", "userPassword", "employeeId", "fullName",
                nationality, gender, "dateOfBirth", "dateOfJoining", "department",
                "employmentStatus", "skillSet", "roleId", "reportingToId", "isDeleted")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
                RETURNING id, "employeeId"
            """
            params = [
                data.get("userName"), data.get("phoneNumber"), encoded_password, employee_id,
                data.get("fullName"), data.get("nationality"), data.get("gender"), data.get("dateOfBirth"),
                data.get("dateOfJoining"), department,
                data.get("employmentStatus"), skill_set_str, user_role, reporting_to_id
            ]

            employee = execute_query(insert_query, params)


            log_activity_raw(
                request=request,
                category='Employee',
                action='Add',
                performer=request.user,
                target_employee_name=employee_name,
                details={
                    'newEmployeeId': employee_id,
                    'newEmployeeName': data.get("fullName")
                }
            )

            employee[0]["generatedPassword"] = generated_password

            return success_response(data=None, message=EMPLOYEE_CREATED_SUCCESSFULLY)

        except Exception as e:
            return error_response(message=f"Error adding employee: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



    def put(self, request, pk=None):
        """
        Updates an employee's details. Only the fields provided in the request
        body will be updated.
        """
        # if not request.user.has_perm('auth.change_user'):
        #     return error_response(message="You don't have permission to update employees", status_code=status.HTTP_403_FORBIDDEN)

        try:
            print("Device-Type Header:", request.headers.get("Device-Type"))
            if pk is None:
                return error_response(message="Employee ID (pk) is required in the URL", status_code=status.HTTP_400_BAD_REQUEST)

            data = request.data
            if not data:
                return error_response(message="Request body cannot be empty", status_code=status.HTTP_400_BAD_REQUEST)

            # These lists will be built dynamically
            set_parts = []
            params = []

            # --- 1. Input Validation (Optional but Recommended) ---
            # Validate against your database CHECK constraints before hitting the DB
            if "gender" in data and data.get("gender") not in ['Male', 'Female']:
                return error_response(message="Invalid gender. Must be 'Male' or 'Female'.", status_code=status.HTTP_400_BAD_REQUEST)

            if "employmentStatus" in data and data.get("employmentStatus") not in ['Active', 'On Leave', 'Resigned', 'Terminated']:
                return error_response(message="Invalid employment status.", status_code=status.HTTP_400_BAD_REQUEST)

            # --- 2. Dynamic Query Building ---

            # Map API field names to their exact database column names (case-sensitive from your schema)
            field_map = {
                # Quoted identifiers (case-sensitive)
                "roleId": '"roleId"',
                "phoneNumber": '"phoneNumber"',
                "fullName": '"fullName"',
                "dateOfBirth": '"dateOfBirth"',
                "dateOfJoining": '"dateOfJoining"',
                "reportingToId": '"reportingToId"',
                "employmentStatus": '"employmentStatus"',
                # Unquoted identifiers (case-insensitive, folded to lowercase)
                "nationality": 'nationality',
                "gender": 'gender',
                "department": 'department'
            }

            # Build the SET clause only for fields present in the request
            for api_field, db_column in field_map.items():
                if api_field in data:
                    set_parts.append(f"{db_column} = %s")
                    params.append(data.get(api_field))

            # --- 3. Handle Special Fields ---

            # Handle skillSet
            if "skillSet" in data:
                skill_set_raw = data["skillSet"]
                skill_set_str = ''  # Default to empty string
                if skill_set_raw and isinstance(skill_set_raw, str) and skill_set_raw.strip():
                    try:
                        skill_set_list = add_new_skills(skill_set_raw, execute_query)
                        skill_set_str = ', '.join(skill_set_list)
                    except ValueError as ve:
                        return error_response(message=str(ve), status_code=status.HTTP_400_BAD_REQUEST)

                set_parts.append('"skillSet" = %s')
                params.append(skill_set_str)

            # Handle password reset/update
            generated_password = None
            if data.get("resetPassword"):
                generated_password = generate_password(3)
                encoded_password = encode_password(generated_password)
                set_parts.append('"userPassword" = %s')
                params.append(encoded_password)
            elif "userPassword" in data and data["userPassword"]:
                encoded_password = encode_password(data["userPassword"])
                set_parts.append('"userPassword" = %s')
                params.append(encoded_password)

            # --- 4. Execute the Query ---

            if not set_parts:
                return error_response(message="No fields to update were provided", status_code=status.HTTP_400_BAD_REQUEST)

            # Join the parts into a final SET clause
            set_clause = ", ".join(set_parts)

            # Add the primary key for the WHERE clause at the very end
            params.append(pk)

            update_query = f"""
                UPDATE "user"
                SET {set_clause}
                WHERE id = %s AND "isDeleted" = 0
                RETURNING "employeeId"
            """

            updated = execute_query(update_query, params, fetch=True, many=False)
            if updated and isinstance(updated, list) and isinstance(updated[0], dict):
                value = list(updated[0].values())[0]  # Get the first (and likely only) value
                # print("updated:", value)
            else:
                print("No value returned")

            if not updated:
                return error_response(message="Employee not found or already deleted", status_code=status.HTTP_404_NOT_FOUND)

            log_activity_raw(
                request=request,
                category='Employee',
                action='Update',
                performer=request.user,
                target_employee_name= value, # The full name of the employee being updated
                details={
                    'updatedFields': list(data.keys())
                }
            )



            # --- 5. Return the Response ---
            device_type = request.headers.get("Device-Type", "").strip()

            if device_type.lower() == "dashboard":
                response_msg = EMPLOYEE_UPDATED_SUCCESSFULLY
            else:
                response_msg = PROFILE_UPDATED_SUCCESSFULLY
            if generated_password:
                return success_response(data={"generatedPassword": generated_password}, message=response_msg)
            else:
                
                return success_response(message=response_msg, status_code=status.HTTP_200_OK)

        except Exception as e:
            # It is highly recommended to log the full exception for debugging
            # import logging
            # logging.error(f"Error updating employee {pk}: {e}", exc_info=True)
            return error_response(message=f"An unexpected error occurred: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



    def delete(self, request):
        user = request.user
        user_ids = request.data.get('userIds')
        if not user_ids or not isinstance(user_ids, list):
            return error_response(message=USER_IDS_MUST_BE_LIST_OF_IDS, status_code=400)

        try:


            User = get_user_model()
            actually_deleted_ids = []
            deleted_employee_names = []

            for user_id in user_ids:
                try:
                    user = User.objects.get(id=user_id, isDeleted='0')

                    # === Logout Logic Here ===
                    print(f"Logging out and invalidating tokens for user {user.userName} (ID: {user.id})")
                    execute_query('UPDATE "user" SET "fcmToken" = NULL WHERE id = %s', [user.id])

                    tokens = OutstandingToken.objects.filter(user=user)
                    for token in tokens:
                        BlacklistedToken.objects.get_or_create(token=token)
                    user.token_invalidated_at = now()
                    user.save(update_fields=["token_invalidated_at"])

                    # === Soft Delete ===
                    execute_query('UPDATE "user" SET "isDeleted" = 1 WHERE id = %s', [user.id])
                    actually_deleted_ids.append(user_id)
                    deleted_employee_names.append(user.fullName if hasattr(user, 'fullName') else user.userName)

                except User.DoesNotExist:
                    continue  # Skip already deleted users

            log_activity_raw(
                request=request,
                category='Employee',
                action='Delete',
                performer=request.user,
                # target_employee_name=user.fullName if hasattr(user, 'fullName') else None,
                details={
                    'deletedEmployeeNames': deleted_employee_names,
                    'deletedUserIds': actually_deleted_ids,
                    'count': len(actually_deleted_ids)
                }
            )

            return success_response(message="Employee(s) deleted successfully")

        except Exception as e:
            return error_response(message=f"Error deleting employees: {str(e)}", status_code=500)

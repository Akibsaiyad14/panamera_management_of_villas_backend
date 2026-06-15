from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import success_response, error_response, execute_query, log_activity_raw
# from api.constants import SHIFT_ADDED_SUCCESSFULLY, SHIFT_UPDATED_SUCCESSFULLY, SHIFTS_FETCHED_SUCCESSFULLY, SHIFTS_DELETED_SUCCESSFULLY

class ShiftView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Fetch all active shifts"""
        try:
            query = """
                SELECT "shiftId", "shiftName", "startTime", "endTime"
                FROM shifts
                WHERE "isDeleted" = 0
                ORDER BY "shiftId" DESC
            """
            shifts = execute_query(query, many=True)
            return success_response(data=shifts, message=SHIFTS_FETCHED_SUCCESSFULLY)
        except Exception as e:
            return error_response(message=f"Error fetching shifts: {str(e)}", status_code=500)

    def post(self, request):
        """Add a new shift"""
        try:
            data = request.data
            required_fields = ["shiftName", "startTime", "endTime"]
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                return error_response(f"Missing fields: {', '.join(missing_fields)}", 400)

            # Check for duplicate shiftName
            check_query = 'SELECT COUNT(*) FROM shifts WHERE "shiftName" = %s AND "isDeleted" = 0'
            count_result = execute_query(check_query, [data["shiftName"]], fetch=True)
            if count_result[0]['count'] > 0:
                return error_response(message="Shift name already exist", status_code=400)

            insert_query = """
                INSERT INTO shifts ("shiftName", "startTime", "endTime")
                VALUES (%s, %s, %s)
                RETURNING "shiftId"
            """
            params = [data["shiftName"], data["startTime"], data["endTime"]]
            new_shift = execute_query(insert_query, params, fetch=True)

            new_shift_id = new_shift[0]['shiftId']

            # --- LOG THE "ADD" ACTIVITY ---
            log_activity_raw(
                request=request,
                category='Shift',
                action='Add',
                performer=request.user,
                target_employee_name=None, # Action is on a shift, not an employee
                target_shift_id=new_shift_id,
                shift_name=data["shiftName"],  # Pass the shift name for logging
                details={
                    'newShiftName': data["shiftName"]
                }
            )


            return success_response(data=new_shift[0], message=SHIFT_ADDED_SUCCESSFULLY)
        except Exception as e:
            return error_response(message=f"Error adding shift: {str(e)}", status_code=500)

    def put(self, request, pk=None):
        """Update shift details"""
        if pk is None:
            return error_response(message="Shift ID is required", status_code=400)
        try:
            data = request.data
            update_query = """
                UPDATE shifts
                SET "shiftName" = %s, "startTime" = %s, "endTime" = %s, "updatedAt" = now()
                WHERE "shiftId" = %s AND "isDeleted" = 0
                RETURNING "shiftId"
            """
            params = [data.get("shiftName"), data.get("startTime"), data.get("endTime"), pk]
            updated_shift = execute_query(update_query, params, fetch=True)
            if not updated_shift:
                return error_response(message="Shift not found or already deleted", status_code=404)

            log_activity_raw(
                request=request,
                category='Shift',
                action='Update',
                performer=request.user,
                target_employee_name=None,
                target_shift_id=pk, # The ID of the shift being updated
                shift_name=data.get("shiftName"),  # Pass the updated shift name for logging
                details={
                    'updatedShiftName': data.get("shiftName"),
                    'updatedFields': list(data.keys())
                }
            )

            return success_response(message=SHIFT_UPDATED_SUCCESSFULLY)
        except Exception as e:
            return error_response(message=f"Error updating shift: {str(e)}", status_code=500)

    def delete(self, request):
        """Soft delete shifts"""
        shift_ids = request.data.get('shiftIds')
        if not shift_ids or not isinstance(shift_ids, list):
            return error_response(message="shiftIds must be a list of IDs", status_code=400)

        try:
            # ✅ Use ANY() for array binding instead of tuple expansion
            query = """
                UPDATE shifts
                SET "isDeleted" = 1
                WHERE "shiftId" = ANY(%s) AND "isDeleted" = 0
                RETURNING "shiftId"
            """
            # Pass Python list directly — psycopg2 will map it to Postgres array
            params = (shift_ids,)
            deleted = execute_query(query, params, fetch=True)

            if not deleted:
                return error_response(
                    message="No matching shifts found or already deleted",
                    status_code=404
                )

            actually_deleted_ids = [s['shiftId'] for s in deleted]

            # --- LOG THE "DELETE" ACTIVITY ---
            log_activity_raw(
                request=request,
                category='Shift',
                action='Delete',
                performer=request.user,
                target_employee_name=None,
                target_shift_id=None,  # This is a bulk action, no single target
                details={
                    'deletedShiftIds': actually_deleted_ids,
                    'count': len(actually_deleted_ids)
                }
            )

            return success_response(message=SHIFTS_DELETED_SUCCESSFULLY)

        except Exception as e:
            return error_response(
                message=f"Error deleting shifts: {str(e)}",
                status_code=500
            )

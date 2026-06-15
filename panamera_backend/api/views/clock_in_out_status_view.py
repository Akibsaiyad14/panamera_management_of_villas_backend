from api.messages import *
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query
from django.utils import timezone
from dateutil.relativedelta import relativedelta # <<< NEW: Import for date calculations

class ClockInOutStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        
        user_id = request.data.get("userId")
        
        try:
            today = timezone.now().date()

            # --- QUERY 1: Get today's live status (This part is correct) ---
            today_records = execute_query("""
                SELECT id, date, "checkInTime", "checkOutTime", "breakInTime", "breakOutTime"
                FROM attendance
                WHERE "labourUserId" = %s AND date = %s AND "isDeleted" = 0
                ORDER BY "createdAt" DESC
                LIMIT 1
            """, [user_id, today])

            shift_updatetd_time = execute_query("""
                SELECT "shiftUpdateTime" FROM "user" WHERE id = %s AND "isDeleted" = 0
            """, [user_id], fetch=True)
            # --- QUERY 2: Get monthly summary statistics (CORRECTED LOGIC) ---

            first_day_of_month = today.replace(day=1)
            # print("First day of month:", first_day_of_month)
            first_day_of_next_month = first_day_of_month + relativedelta(months=1)
            # print("First day of next month:", first_day_of_next_month)

            # <<< FIX: The query is explicitly defined here to ensure it's correct.
            # It MUST filter by labourUserId.
            summary_query = """
                                SELECT
                                    COALESCE(COUNT(DISTINCT date) FILTER (WHERE "attendanceStatus" IN ('Halfday', 'Normal', 'Overtime')), 0) AS "workingDays",
                                    COALESCE(COUNT(DISTINCT date) FILTER (WHERE "attendanceStatus" = 'Absent'), 0) AS "absentDays",
                                    COALESCE(SUM("overtimeHours") FILTER (WHERE "overtimeStatus" IN (2, 3)), 0.0) AS "totalOvertime",
                                    COALESCE(COUNT(DISTINCT date) FILTER (WHERE "attendanceStatus" IN ('Sick Leave', 'Annual Leave', 'Paid Leave', 'Paid Holiday', 'Public Holiday', 'Day Off')), 0) AS "holidays"
                                FROM attendance
                                WHERE
                                    "labourUserId" = %s
                                    AND date >= %s
                                    AND date < %s
                                    AND "isDeleted" = 0;
                            """

            # <<< FIX: The parameters MUST match the query's placeholders exactly.
            summary_params = [user_id, first_day_of_month, first_day_of_next_month]
            # print("Summary Query:", summary_query)

            # For debugging, you can print this right before the query runs:
            # print("Executing summary query:", summary_query)
            # print("With parameters:", summary_params)

            summary_data_list = execute_query(summary_query, summary_params, fetch=True)
            # print("Summary Data List:", summary_data_list)

            summary_data = summary_data_list[0] if summary_data_list else {}

            # --- Build the final response ---
            response_data = {
                "attendanceId": None,
                "date": today.strftime("%Y-%m-%d"),
                "clockInStatus": False,
                "clockInTime": None,
                "clockOutStatus": False,
                "clockOutTime": None,
                "breakStatus": "NOT_AVAILABLE",
                "breakInTime": None,
                "breakOutTime": None,
                "shiftUpdateTime": (
                    shift_updatetd_time[0]["shiftUpdateTime"].strftime("%Y-%m-%d %H:%M:%S")
                    if shift_updatetd_time and shift_updatetd_time[0]["shiftUpdateTime"]
                    else None),
                "workingDaysInMonth": summary_data.get("workingDays", 0),
                "absentDaysInMonth": summary_data.get("absentDays", 0),
                "totalOvertimeInMonth": float(summary_data.get("totalOvertime", 0.0)),
                "holidaysInMonth": summary_data.get("holidays", 0),
            }

            if today_records:
                record = today_records[0]
                response_data["attendanceId"] = record["id"]

                if record["checkInTime"]:
                    response_data["clockInStatus"] = True
                    response_data["clockInTime"] = record["checkInTime"].strftime("%Y-%m-%d %H:%M:%S")
                    response_data["breakStatus"] = "AVAILABLE_TO_START"

                if record["breakInTime"] and not record["breakOutTime"]:
                    response_data["breakStatus"] = "ON_BREAK"
                    response_data["breakInTime"] = record["breakInTime"].strftime("%Y-%m-%d %H:%M:%S")

                elif record["breakInTime"] and record["breakOutTime"]:
                    response_data["breakStatus"] = "COMPLETED"
                    response_data["breakInTime"] = record["breakInTime"].strftime("%Y-%m-%d %H:%M:%S")
                    response_data["breakOutTime"] = record["breakOutTime"].strftime("%Y-%m-%d %H:%M:%S")

                if record["checkOutTime"]:
                    response_data["clockOutStatus"] = True
                    response_data["clockOutTime"] = record["checkOutTime"].strftime("%Y-%m-%d %H:%M:%S")
                    if response_data["breakStatus"] != "COMPLETED":
                         response_data["breakStatus"] = "NOT_AVAILABLE"

            return success_response(
                data=response_data,
                message=STATUS_MONTHLY_SUMMARY_FETCHED_SUCCESSFULLY,
                status_code=200
            )

        except Exception as e:
            return error_response(message=f"{ERROR_FETCHING_ATTENDANCE_STATUS}: {str(e)}", status_code=500)

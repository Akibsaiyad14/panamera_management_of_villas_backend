from api.messages import (
    NO_ABSENT_ATTENDANCE_RECORDS_FOUND,
    ABSENT_ATTENDANCE_RECORDS_RETRIEVED_SUCCESSFULLY,
)
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status
from api.utils import success_response, error_response, execute_query

class GetAbsentAttendanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # Query to fetch attendance records where attendanceStatus is 'Absent' and not deleted,
            # restricted to active employees only
            query = """
                SELECT
                    a.id,
                    a."labourUserId",
                    a.date,
                    a."checkInTime",
                    a."checkOutTime",
                    a."effectiveCheckoutTime",
                    a."assignedShiftAtCheckInId",
                    a."calculatedRegularHours",
                    a."checkInLatitude",
                    a."checkInLongitude",
                    a."checkOutLatitude",
                    a."checkOutLongitude",
                    a.status,
                    a."createdAt",
                    a."updatedAt",
                    a."sessionNumber",
                    a."overtimeHours",
                    a."overtimeStatus",
                    a."attendanceStatus",
                    a."breakInTime",
                    a."breakOutTime",
                    a."checkoutType",
                    a."earlyReason",
                    a."checkInDeviceId",
                    a."checkOutDeviceId"
                FROM "attendance" a
                JOIN "user" u ON a."labourUserId" = u.id
                WHERE a."attendanceStatus" = %s
                  AND a."isDeleted" = 0
                  AND u."isDeleted" = 0
                  AND u."employmentStatus" = 'Active'
            """

            # Execute query with 'Absent' as the parameter
            results = execute_query(query, ['Absent'], many=True)

            if not results:
                return success_response(
                    message=NO_ABSENT_ATTENDANCE_RECORDS_FOUND,
                    data=[],
                    status_code=drf_status.HTTP_200_OK
                )

            return success_response(
                message=ABSENT_ATTENDANCE_RECORDS_RETRIEVED_SUCCESSFULLY,
                data=results,
                status_code=drf_status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(
                message=f"Error retrieving absent attendance records: {str(e)}",
                status_code=drf_status.HTTP_500_INTERNAL_SERVER_ERROR
            )

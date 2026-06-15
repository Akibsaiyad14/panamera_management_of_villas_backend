"""
Monthly Attendance Report API
Provides a comprehensive monthly attendance report showing daily status for each employee
"""
from datetime import datetime, timedelta
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response
from rest_framework import status as http_status
from calendar import monthrange


class MonthlyAttendanceReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # Get parameters
            today = datetime.now()
            month = int(request.GET.get('month', today.month))
            year = int(request.GET.get('year', today.year))
            employee_ids_param = request.GET.get('employeeIds', '')
            department_id = request.GET.get('departmentId', '')
            is_export = request.GET.get('isExport', 'false').lower() == 'true'
            
            # Pagination parameters
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('pageSize', 50))
            
            # Validation
            if page < 1:
                return error_response(message="Page number must be greater than 0.", status_code=http_status.HTTP_400_BAD_REQUEST)
            if page_size < 1 or page_size > 500:
                return error_response(message="Page size must be between 1 and 500.", status_code=http_status.HTTP_400_BAD_REQUEST)
            if not (1 <= month <= 12):
                return error_response(message="Invalid month.", status_code=http_status.HTTP_400_BAD_REQUEST)
            if not (2000 <= year <= 2100):
                return error_response(message="Invalid year.", status_code=http_status.HTTP_400_BAD_REQUEST)

            num_days = monthrange(year, month)[1]
            start_date = datetime(year, month, 1).date()
            end_date = datetime(year, month, num_days).date()

            # Build filters
            employee_filter = ' AND u."roleId" NOT IN (1, 2, 3)'
            params = []
            
            if employee_ids_param:
                employee_ids = [eid.strip() for eid in employee_ids_param.split(',') if eid.strip()]
                if employee_ids:
                    placeholders = ','.join(['%s'] * len(employee_ids))
                    employee_filter = f' AND u.id IN ({placeholders})'
                    params.extend(employee_ids)
            
            if department_id:
                employee_filter += ' AND u.department = %s'
                params.append(department_id)

            # Get total count
            count_query = f'SELECT COUNT(*) as total FROM "user" u WHERE u."isDeleted" = 0 AND u."employmentStatus" = \'Active\' {employee_filter}'
            count_result = execute_query(count_query, params, many=False)
            total_records = count_result[0]['total'] if count_result else 0
            
            # Employee Query Logic
            if is_export:
                # Update report view metadata
                update_report_time_query = 'UPDATE public."reportsViews" SET "dateTime" = %s WHERE id = 2'
                execute_query(update_report_time_query, [datetime.now()], many=False)
                
                employees_query = f"""
                    SELECT u.id as "employeeId", u."employeeId" as "employeeCode", 
                           u."fullName" as name, u.department FROM "user" u
                    WHERE u."isDeleted" = 0 AND u."employmentStatus" = 'Active' {employee_filter} ORDER BY u."employeeId"
                """
                employees = execute_query(employees_query, params, many=True)
                total_pages = 1
            else:
                offset = (page - 1) * page_size
                total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 0
                employees_query = f"""
                    SELECT u.id as "employeeId", u."employeeId" as "employeeCode", 
                           u."fullName" as name, u.department FROM "user" u
                    WHERE u."isDeleted" = 0 AND u."employmentStatus" = 'Active' {employee_filter} 
                    ORDER BY u."employeeId" LIMIT %s OFFSET %s
                """
                employees = execute_query(employees_query, params + [page_size, offset], many=True)
            
            if not employees:
                return success_response(data={'results': []}, message="No records found.")

            # Get attendance records
            employee_ids_list = [emp['employeeId'] for emp in employees]
            placeholders = ','.join(['%s'] * len(employee_ids_list))
            attendance_query = f"""
                SELECT "labourUserId" as "employeeId", date, "attendanceStatus", "overtimeHours"
                FROM attendance WHERE date BETWEEN %s AND %s
                AND "labourUserId" IN ({placeholders}) AND "isDeleted" = 0
            """
            attendance_records = execute_query(attendance_query, [start_date, end_date] + employee_ids_list, many=True)

            # Map records: map[emp_id][date_obj] = record
            attendance_map = {}
            for record in attendance_records:
                emp_id = record['employeeId']
                attendance_map.setdefault(emp_id, {})[record['date']] = record

            report_data = []
            for employee in employees:
                emp_id = employee['employeeId']
                daily_status = {}
                
                # Counters
                present_count = 0
                absent_count = 0
                day_off_count = 0
                sick_leave_count = 0
                public_holiday_count = 0
                annual_leave_count = 0
                emergency_leave_count = 0
                overtime_hours = 0

                for day in range(1, num_days + 1):
                    current_date = datetime(year, month, day).date()
                    day_key = f"{day:02d}-{current_date.strftime('%b')}"
                    
                    if emp_id in attendance_map and current_date in attendance_map[emp_id]:
                        record = attendance_map[emp_id][current_date]
                        att_status = (record.get('attendanceStatus') or '').upper()

                        # Status mapping
                        if att_status == 'OVERTIME':
                            daily_status[day_key] = 'OT'
                            overtime_hours += record.get('overtimeHours', 0) or 0
                            present_count += 1
                        elif att_status in ['PRESENT', 'NORMAL']:
                            daily_status[day_key] = 'P'
                            present_count += 1
                        elif att_status == 'ABSENT':
                            daily_status[day_key] = 'A'
                            absent_count += 1
                        elif att_status == 'DAY OFF':
                            daily_status[day_key] = 'DO'
                            day_off_count += 1
                        elif att_status == 'SICK LEAVE':
                            daily_status[day_key] = 'SL'
                            sick_leave_count += 1
                        elif att_status == 'PUBLIC HOLIDAY':
                            daily_status[day_key] = 'PH'
                            public_holiday_count += 1
                        elif att_status == 'ANNUAL LEAVE':
                            daily_status[day_key] = 'AL'
                            annual_leave_count += 1
                        elif att_status == 'EMERGENCY LEAVE':
                            daily_status[day_key] = 'EL'
                            emergency_leave_count += 1
                        else:
                            daily_status[day_key] = 'A'
                            absent_count += 1
                    else:
                        if current_date <= today.date():
                            daily_status[day_key] = 'A'
                            absent_count += 1
                        else:
                            daily_status[day_key] = '-'
                
                report_data.append({
                    'employeeId': employee['employeeCode'] or emp_id,
                    'name': employee['name'],
                    'department': employee['department'],
                    'dailyStatus': daily_status,
                    'summary': {
                        'totalPresent': present_count,
                        'totalAbsent': absent_count,
                        'totalDayOff': day_off_count,
                        'totalSickLeave': sick_leave_count,
                        'totalPublicHoliday': public_holiday_count,
                        'totalAnnualLeave': annual_leave_count,
                        'totalEmergencyLeave': emergency_leave_count,
                        'totalOvertimeHours': overtime_hours,
                    }
                })

            return success_response(
                data={
                    'results': report_data,
                    'pagination': {
                        'currentPage': page,
                        'pageSize': page_size,
                        'totalRecords': total_records,
                        'totalPages': total_pages
                    },
                },
                message="Monthly attendance report retrieved successfully."
            )

        except Exception as e:
            return error_response(message=str(e), status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR)

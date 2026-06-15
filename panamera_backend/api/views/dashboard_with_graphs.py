from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from datetime import datetime, timedelta, date
from calendar import monthrange
from api.utils import execute_query, success_response, error_response
from api.constants import AMC_STATUS_ACTIVE


class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Dashboard Summary API
        Filters by an optional date range (?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD)
        """
        try:
            start_date = request.query_params.get("startDate")
            end_date = request.query_params.get("endDate")

            current_date = date.today()

            # status_filter_sql = """
            #     AND m."status" = 0 
            #     AND m."isDeleted" = 0 
            #     AND cust."status" = 0 
            #     AND COALESCE(cust."isDeleted", 0) = 0
            # """


            # Build WHERE clause dynamically for tasks and issues (on createdAt)
            task_date_filter = ""
            if start_date and end_date:
                task_date_filter = f"AND DATE(t.\"startDate\") BETWEEN '{start_date}' AND '{end_date}'"
            elif start_date:
                task_date_filter = f"AND DATE(t.\"startDate\") >= '{start_date}'"
            elif end_date:
                task_date_filter = f"AND DATE(t.\"startDate\") <= '{end_date}'"

            # Build WHERE clause dynamically for AMC Jobs (on visitDate)
            amc_date_filter = ""
            if start_date and end_date:
                amc_date_filter = f"AND DATE(aj.\"visitDate\") BETWEEN '{start_date}' AND '{end_date}'"
            elif start_date:
                amc_date_filter = f"AND DATE(aj.\"visitDate\") >= '{start_date}'"
            elif end_date:
                amc_date_filter = f"AND DATE(aj.\"visitDate\") <= '{end_date}'"

            # -------------------- 1️⃣ Active AMC Jobs --------------------
            amc_jobs_query = f"""
                SELECT 
                    COUNT(DISTINCT CASE WHEN aj."visitType"=1 THEN aj."amcJobId" END) AS garden_count,
                    COUNT(DISTINCT CASE WHEN aj."visitType"=2 THEN aj."amcJobId" END) AS pool_count,
                    COUNT(DISTINCT CASE WHEN aj."visitStatus" = 2 AND aj."visitType"=1 THEN aj."amcJobId" END) AS completed_garden,
                    COUNT(DISTINCT CASE WHEN aj."visitStatus" = 2 AND aj."visitType"=2 THEN aj."amcJobId" END) AS completed_pool
                FROM "AmcJobs" aj
                JOIN "AMCMaster" m ON aj."amcId" = m."amcId"
                WHERE aj."visitDate" IS NOT NULL
                  AND m."status" = {AMC_STATUS_ACTIVE}
                  AND m."isDeleted" = 0
                  {amc_date_filter};
            """
            amc_jobs_result = execute_query(amc_jobs_query, fetch="one")
            completed_amc_jobs_garden = amc_jobs_result[0]["completed_garden"] if amc_jobs_result else 0
            completed_amc_jobs_pool = amc_jobs_result[0]["completed_pool"] if amc_jobs_result else 0
            activ_garden_count = amc_jobs_result[0]["garden_count"] if amc_jobs_result else 0
            activ_pool_count = amc_jobs_result[0]["pool_count"] if amc_jobs_result else 0

            # -------------------- 2️⃣ Active Tasks --------------------
            # Show ALL tasks that are not closed, including overdue ones
            active_tasks_query = """
                SELECT COUNT(*) AS count
                FROM "taskManager" t
                WHERE t."isDeleted" = 0
                  AND t."taskType" = 0
                  AND t."taskStatus" != 1;
            """
            active_tasks_result = execute_query(active_tasks_query, fetch="one")
            active_tasks_count = active_tasks_result[0]["count"] if active_tasks_result else 0

            # -------------------- 3️⃣ Active Issues --------------------
            # Show ALL issues that are not closed, including overdue ones
            active_issues_query = """
                SELECT COUNT(*) AS count
                FROM "taskManager" t
                WHERE t."isDeleted" = 0
                  AND t."taskType" = 1
                  AND t."taskStatus" != 1;
            """
            active_issues_result = execute_query(active_issues_query, fetch="one")
            active_issues_count = active_issues_result[0]["count"] if active_issues_result else 0

            # -------------------- 4️⃣ Tasks Due Today --------------------
            tasks_due_today_query = """
                SELECT COUNT(*) AS count
                FROM "taskManager"
                WHERE "isDeleted" = 0
                  AND "taskType" = 0
                  AND "taskStatus" != 1
                  AND "dueDate" = CURRENT_DATE;
            """
            tasks_due_today_result = execute_query(tasks_due_today_query, fetch="one")
            tasks_due_today_count = tasks_due_today_result[0]["count"] if tasks_due_today_result else 0

            # -------------------- 5️⃣ Issues Due Today --------------------
            issues_due_today_query = """
                SELECT COUNT(*) AS count
                FROM "taskManager"
                WHERE "isDeleted" = 0
                  AND "taskType" = 1
                  AND "taskStatus" != 1
                  AND "dueDate" = CURRENT_DATE;
            """
            issues_due_today_result = execute_query(issues_due_today_query, fetch="one")
            issues_due_today_count = issues_due_today_result[0]["count"] if issues_due_today_result else 0


            # -------------------- 6️⃣ Checked-in Users (from eligible users) --------------------
            checked_in_query = f"""
                SELECT COUNT(DISTINCT a."labourUserId") AS count
                FROM attendance a
                JOIN "user" u ON a."labourUserId" = u.id
                WHERE a."isDeleted" = 0
                  AND a.date = '{current_date}'
                  AND a."checkInTime" IS NOT NULL
                  AND u."isDeleted" = 0
                  AND u."employmentStatus" = 'Active'
                  AND u."roleId" NOT IN (1, 2, 3);
            """
            checked_in_result = execute_query(checked_in_query, fetch="one")
            checked_in_count = checked_in_result[0]["count"] if checked_in_result else 0

            # -------------------- 7️⃣ Absent Users (from eligible users) --------------------
            # Absent users are eligible users who DO NOT have a check-in record for today
            absent_users_query = f"""
                SELECT COUNT(u.id) AS count
                FROM "user" u
                LEFT JOIN attendance a ON u.id = a."labourUserId" AND a.date = '{current_date}' AND a."checkInTime" IS NOT NULL
                WHERE u."isDeleted" = 0
                  AND u."employmentStatus" = 'Active'
                  AND u."roleId" NOT IN (1, 2, 3)
                  AND a.id IS NULL; -- This condition means no matching attendance record with checkInTime for today
            """
            absent_users_result = execute_query(absent_users_query, fetch="one")
            absent_users_count = absent_users_result[0]["count"] if absent_users_result else 0
            # -------------------- Assemble Response --------------------
            dashboard_data = {
                "completedAmcJobsGarden": completed_amc_jobs_garden,
                "completedAmcJobsPool": completed_amc_jobs_pool,
                "activeAmcJobsGarden": activ_garden_count,
                "activeAmcJobsPool": activ_pool_count,
                "activeTasks": active_tasks_count,
                "activeIssues": active_issues_count,
                "tasksDueToday": tasks_due_today_count,
                "issuesDueToday": issues_due_today_count,
                "checkedInUsers": checked_in_count,
                "absentUsers": absent_users_count,
            }

            return success_response(
                data=dashboard_data,
                message="Dashboard data fetched successfully",
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            return error_response(
                message=f"Error fetching dashboard data: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )



class DashboardGraphsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # -----------------------------
            # 1. Parse query params
            # -----------------------------
            year_val = request.query_params.get("year")
            month_val = request.query_params.get("month")
            day_val = request.query_params.get("day")
            view_type = request.query_params.get("view_type", "monthly") # Default to 'monthly'

            try:
                year = int(year_val) if year_val else None
                month = int(month_val) if month_val else None
                day = int(day_val) if day_val else None
            except (ValueError, TypeError):
                return error_response("year, month, day must be integers")

            # -----------------------------
            # 2. Validate and adjust view_type/parameters
            # -----------------------------
            if view_type == "yearly":
                if not year:
                    return error_response("year is required for yearly view")
                if month or day:
                    print(f"DEBUG: Month ({month}) or day ({day}) parameters are ignored for 'yearly' view.")
                month = None
                day = None
            elif view_type == "monthly":
                if year and not month:
                    view_type = "yearly_by_month"
                    day = None
                elif not (year and month):
                    return error_response("year and month are required for monthly view")
                if month and not (1 <= month <= 12):
                    return error_response("month must be 1-12")
                if day:
                    _, last_day_of_month_for_validation = monthrange(year, month)
                    if not (1 <= day <= last_day_of_month_for_validation):
                        return error_response(f"invalid day {day} for month {month}")
            else:
                return error_response("invalid view_type. Must be 'monthly' or 'yearly'")

            # -----------------------------
            # 3. Build date range and base SQL
            # -----------------------------
            sql_group_by_period_clause = ""
            python_date_format_str = ""
            params = {}
            where_parts = ['"isDeleted" = 0']
            all_periods_list = []

            if view_type == "yearly" or view_type == "yearly_by_month":
                start_date = datetime(year, 1, 1).date()
                end_date = datetime(year, 12, 31).date()
                sql_group_by_period_clause = 'DATE_TRUNC(\'month\', "createdAt")'
                python_date_format_str = "%Y-%m"
                
                for m in range(1, 13):
                    all_periods_list.append(datetime(year, m, 1).strftime(python_date_format_str))

            elif view_type == "monthly":
                _, last_day_of_month = monthrange(year, month)
                start_date = datetime(year, month, 1).date()
                end_date = datetime(year, month, last_day_of_month).date()
                sql_group_by_period_clause = 'DATE("createdAt")'
                python_date_format_str = "%Y-%m-%d"

                if day:
                    all_periods_list.append(datetime(year, month, day).strftime(python_date_format_str))
                    where_parts.append('EXTRACT(DAY FROM "createdAt") = %(day)s')
                    params['day'] = day
                else:
                    current = start_date
                    while current <= end_date:
                        all_periods_list.append(current.strftime(python_date_format_str))
                        current += timedelta(days=1)
            
            params['start'] = start_date
            params['end'] = end_date + timedelta(days=1)
            where_parts.append('DATE("createdAt") >= %(start)s')
            where_parts.append('DATE("createdAt") < %(end)s')

            where_clause = "WHERE " + " AND ".join(where_parts)

            # -----------------------------
            # 4. Query: Group by the determined period
            # -----------------------------
            sql = f"""
                SELECT
                    "taskType",
                    {sql_group_by_period_clause} AS period,
                    COUNT(*) AS registered, -- This now counts all created within the period
                    COUNT(*) FILTER (WHERE "taskStatus" = 1) AS completed
                FROM "taskManager"
                {where_clause}
                GROUP BY "taskType", period
                ORDER BY period;
            """

            raw_data_result = execute_query(sql, params=params, fetch=True, many=True)
            
            if raw_data_result is False:
                return error_response("Database query failed.")
            elif raw_data_result is True:
                raw_data = []
            else:
                raw_data = raw_data_result

            # -----------------------------
            # 5. Build final dict with 0s
            # -----------------------------
            def build_stats(task_type):
                # Initialize both registered and completed with 0s for all periods
                registered_counts = {p: 0 for p in all_periods_list}
                completed_counts = {p: 0 for p in all_periods_list}

                for row in raw_data:
                    if row["taskType"] != task_type:
                        continue
                    
                    db_period = row["period"]
                    
                    if isinstance(db_period, datetime):
                        period_str = db_period.strftime(python_date_format_str)
                    elif isinstance(db_period, date):
                        period_str = db_period.strftime(python_date_format_str)
                    else: 
                        print(f"WARNING: Unexpected period type from DB: {type(db_period)} for row {row}")
                        continue

                    if period_str in registered_counts:
                        registered_counts[period_str] = row["registered"] # Use the new 'registered' column from SQL
                    if period_str in completed_counts:
                        completed_counts[period_str] = row["completed"]

                return {"registered": registered_counts, "completed": completed_counts}

            result = {
                "tasks": build_stats(0),
                "issues": build_stats(1)
            }

            return success_response(data=result, message="Statistics fetched successfully")

        except Exception as e:
            import traceback
            traceback.print_exc()
            return error_response(f"Error: {str(e)}", status.HTTP_500_INTERNAL_SERVER_ERROR)

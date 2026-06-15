import datetime
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
import pytz
from api.utils import execute_query, success_response, error_response, log_activity_raw
from api.constants import *


class TaskIssueReportView(APIView):
    """
    Analytics API for combined Tasks & Issues reporting dashboard.

    Query Parameters:
        startDate: YYYY-MM-DD (required)
        endDate: YYYY-MM-DD (required)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            dubai_tz = pytz.timezone('Asia/Dubai')
            now_dubai = datetime.datetime.now(dubai_tz)
            start_date_str = (request.query_params.get("startDate") or "").strip()
            end_date_str = (request.query_params.get("endDate") or "").strip()
            is_export = request.query_params.get("isExport", "false").lower() == "true"

            

            if not start_date_str or not end_date_str:
                return error_response(
                    message="startDate and endDate are required. Use YYYY-MM-DD format.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                start_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
                end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
            except ValueError:
                return error_response(
                    message="Invalid date format. Use YYYY-MM-DD.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if start_date > end_date:
                return error_response(
                    message="startDate cannot be after endDate.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            
            if is_export:
                log_activity_raw(
                    request=request,
                    category="Export",
                    action="TaskIssueReport",
                    performer=request.user,
                    details={
                        "startDate": start_date_str,
                        "endDate": end_date_str,
                        "filtersUsed": dict(request.query_params),
                    },
                )
                update_report_time_query = """
                    UPDATE public."reportsViews"
                    SET "dateTime" = %s
                    WHERE id = 4
                """
                execute_query(update_report_time_query, [now_dubai], many=False)
                return success_response(
                    message="Report export initiated. Download will be available shortly.",
                    status_code=status.HTTP_200_OK
                )


            prev_start, prev_end = self._get_previous_range(start_date, end_date)

            summary, source_breakdown, status_breakdown = self._get_summary_and_breakdowns(
                start_date, end_date, prev_start, prev_end
            )
            issue_sources = self._get_issue_sources(start_date, end_date)
            work_items = self._get_work_items(start_date, end_date)
            supervisor_performance = self._get_supervisor_performance(start_date, end_date)
            team_leader_performance = self._get_team_leader_performance(start_date, end_date)

            report_data = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "summary": summary,
                "sourceBreakdown": source_breakdown,
                "statusBreakdown": status_breakdown,
                "issueSources": issue_sources,
                "workItems": work_items,
                "supervisorPerformance": supervisor_performance,
                "teamLeaderPerformance": team_leader_performance,
            }

            return success_response(
                data=report_data,
                message="Task & Issue Report fetched successfully",
            )
        except Exception as exc:
            return error_response(
                message=f"Error fetching Task & Issue Report: {str(exc)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def _get_previous_range(start_date, end_date):
        delta = end_date - start_date + datetime.timedelta(days=1)
        prev_end = start_date - datetime.timedelta(days=1)
        prev_start = prev_end - delta + datetime.timedelta(days=1)
        return prev_start, prev_end

    @staticmethod
    def _fmt_datetime_short(dt_value):
        if not dt_value:
            return None
        if isinstance(dt_value, datetime.date) and not isinstance(dt_value, datetime.datetime):
            dt_value = datetime.datetime.combine(dt_value, datetime.time.min)
        return dt_value.strftime("%b %d, %H:%M")

    @staticmethod
    def _safe_num(value, digits=1):
        return round(float(value or 0), digits)

    @staticmethod
    def _build_work_item_id(task_type, row_id):
        return row_id

    @staticmethod
    def _progress_buckets(today_date):
        return f'''
            CASE
                WHEN t."taskStatus" = {CLOSED} THEN 'resolved'
                WHEN t."taskStatus" <> {CLOSED} AND t."dueDate" IS NOT NULL AND t."dueDate"::date < DATE %s THEN 'overdue'
                WHEN t."taskStatus" = {IN_PROGRESS} THEN 'inProgress'
                WHEN t."taskStatus" IN ({OPEN}, {ON_HOLD}, {AWAITING_GATE_PASS}) THEN 'pending'
                ELSE 'pending'
            END
        ''', [today_date]

    def _period_snapshot(self, start_date, end_date):
        today_date = datetime.date.today()
        query = f'''
            WITH filtered AS (
                SELECT
                    t.id,
                    t."taskType",
                    t."taskStatus",
                    t."dueDate",
                    COALESCE(t."wasRequested", FALSE) AS "wasRequested",
                    COALESCE(t."createdAt", t."startDate") AS "startAt",
                    t."lastStatusDate" AS "resolvedAt"
                FROM "taskManager" t
                WHERE COALESCE(t."isDeleted", 0) = 0
                  AND COALESCE(t."approvalStatus", 0) = 1
                  AND t."taskType" IN ({TASK}, {ISSUE})
                  AND t."startDate"::date BETWEEN %s AND %s
            )
            SELECT
                COUNT(*) FILTER (WHERE "taskType" = {ISSUE}) AS "totalIssues",
                COUNT(*) FILTER (WHERE "taskType" = {TASK}) AS "totalTasks",
                COUNT(*) FILTER (WHERE "taskType" = {ISSUE} AND "wasRequested" = TRUE) AS "customerRaisedIssues",
                COUNT(*) FILTER (WHERE "taskType" = {ISSUE} AND "wasRequested" = FALSE) AS "teamLeaderRaisedIssues",
                COUNT(*) FILTER (WHERE "taskStatus" = {CLOSED}) AS "resolved",
                COUNT(*) FILTER (WHERE "taskStatus" = {IN_PROGRESS}) AS "inProgress",
                COUNT(*) FILTER (WHERE "taskStatus" IN ({OPEN}, {ON_HOLD}, {AWAITING_GATE_PASS})) AS "pending",
                COUNT(*) FILTER (
                    WHERE "taskStatus" <> {CLOSED}
                      AND "dueDate" IS NOT NULL
                      AND "dueDate"::date < DATE %s
                ) AS "overdue",
                COALESCE(AVG(
                CASE
                    WHEN "taskStatus" = {CLOSED}
                    AND "resolvedAt" IS NOT NULL
                    AND "startAt" IS NOT NULL
                    AND "resolvedAt" >= "startAt"
                    THEN EXTRACT(EPOCH FROM ("resolvedAt" - "startAt")) / 3600.0
                    ELSE NULL
                END
            ), 0) AS "avgResolveHrs"
            FROM filtered
        '''
        params = [start_date, end_date, today_date]
        row = execute_query(query, params, many=False)
        if isinstance(row, list):
            return row[0] if row else {}
        return row or {}

    def _get_summary_and_breakdowns(self, start_date, end_date, prev_start, prev_end):
        current = self._period_snapshot(start_date, end_date)
        previous = self._period_snapshot(prev_start, prev_end)

        total_issues = int(current.get("totalIssues") or 0)
        total_tasks = int(current.get("totalTasks") or 0)
        customer_raised = int(current.get("customerRaisedIssues") or 0)
        resolved = int(current.get("resolved") or 0)
        in_progress = int(current.get("inProgress") or 0)
        pending = int(current.get("pending") or 0)
        overdue = int(current.get("overdue") or 0)
        total_items = total_issues + total_tasks

        completion_rate = round((resolved / total_items) * 100, 1) if total_items > 0 else 0
        customer_ratio = round((customer_raised / total_issues) * 100, 0) if total_issues > 0 else 0

        prev_total_issues = int(previous.get("totalIssues") or 0)
        prev_total_tasks = int(previous.get("totalTasks") or 0)
        prev_resolved = int(previous.get("resolved") or 0)
        prev_total_items = prev_total_issues + prev_total_tasks
        prev_completion_rate = round((prev_resolved / prev_total_items) * 100, 1) if prev_total_items > 0 else 0

        avg_resolve_hrs = self._safe_num(current.get("avgResolveHrs"), 1)
        prev_avg_resolve_hrs = self._safe_num(previous.get("avgResolveHrs"), 1)

        summary = {
            "totalIssues": total_issues,
            "totalIssuesChange": total_issues - prev_total_issues,
            "totalTasks": total_tasks,
            "totalTasksChange": total_tasks - prev_total_tasks,
            "customerRaisedIssues": customer_raised,
            "customerRaisedRatio": customer_ratio,
            "avgResolveHrs": avg_resolve_hrs,
            "avgResolveHrsChange": round(avg_resolve_hrs - prev_avg_resolve_hrs, 1),
            "overdueItems": overdue,
            "overdueItemsChange": overdue - int(previous.get("overdue") or 0),
            "completionRate": completion_rate,
            "completionRateChange": round(completion_rate - prev_completion_rate, 1),
        }

        source_breakdown = {
            "customer": customer_raised,
            "teamLeader": int(current.get("teamLeaderRaisedIssues") or 0),
        }

        status_breakdown = {
            "resolved": resolved,
            "inProgress": in_progress,
            "pending": pending,
            "overdue": overdue,
        }

        return summary, source_breakdown, status_breakdown

    def _get_issue_sources(self, start_date, end_date):
        query = f'''
            SELECT
                COALESCE(su."fullName", su.name, su."userName", t."supervisorId") AS "name",
                COUNT(*) FILTER (WHERE COALESCE(t."wasRequested", FALSE) = TRUE) AS "customer",
                COUNT(*) FILTER (WHERE COALESCE(t."wasRequested", FALSE) = FALSE) AS "teamLeader"
            FROM "taskManager" t
            LEFT JOIN "user" su
                ON su."employeeId" = t."supervisorId"
               AND COALESCE(su."isDeleted", 0) = 0
            WHERE COALESCE(t."isDeleted", 0) = 0
              AND COALESCE(t."approvalStatus", 0) = 1
              AND t."taskType" = {ISSUE}
              AND t."startDate"::date BETWEEN %s AND %s
            GROUP BY COALESCE(su."fullName", su.name, su."userName", t."supervisorId")
            ORDER BY (COUNT(*) FILTER (WHERE COALESCE(t."wasRequested", FALSE) = TRUE) +
                      COUNT(*) FILTER (WHERE COALESCE(t."wasRequested", FALSE) = FALSE)) DESC,
                     "name" ASC
            LIMIT 5
        '''
        rows = execute_query(query, [start_date, end_date], many=True) or []
        return [
            {
                "name": row.get("name"),
                "customer": int(row.get("customer") or 0),
                "teamLeader": int(row.get("teamLeader") or 0),
            }
            for row in rows
        ]

    def _get_work_items(self, start_date, end_date):
        query = '''
            SELECT
                t.id,
                t."requestId",
                t."taskType",
                t."taskName",
                t."priority",
                t."taskStatus",
                COALESCE(t."wasRequested", FALSE) AS "wasRequested",
                t."startDate",
                t."createdAt",
                t."lastStatusDate" AS "resolvedAt",
                COALESCE(su."fullName", su.name, su."userName", t."supervisorId") AS "assignedSupervisor",
                COALESCE(tl."fullName", tl.name, tl."userName", t."teamLeaderId") AS "assignedTeamLeader"
            FROM "taskManager" t
            LEFT JOIN "user" su
                ON su."employeeId" = t."supervisorId"
               AND COALESCE(su."isDeleted", 0) = 0
            LEFT JOIN "user" tl
                ON tl."employeeId" = t."teamLeaderId"
               AND COALESCE(tl."isDeleted", 0) = 0
            WHERE COALESCE(t."isDeleted", 0) = 0
              AND COALESCE(t."approvalStatus", 0) = 1
              AND t."taskType" IN (%s, %s)
              AND t."startDate"::date BETWEEN %s AND %s
            ORDER BY t."startDate" DESC, t.id DESC
            LIMIT 100
            
        '''
        rows = execute_query(query, [TASK, ISSUE, start_date, end_date], many=True) or []

        items = []
        for row in rows:
            item_status = int(row.get("taskStatus") or 0)
            resolved_at = row.get("resolvedAt") if item_status == CLOSED else None
            start_at = row.get("createdAt") or row.get("startDate")

            resolve_hrs = None
            if resolved_at and start_at:
                start_dt = start_at
                if isinstance(start_dt, datetime.date) and not isinstance(start_dt, datetime.datetime):
                    start_dt = datetime.datetime.combine(start_dt, datetime.time.min)
                diff_seconds = (resolved_at - start_dt).total_seconds()
                if diff_seconds >= 0:
                    resolve_hrs = round(diff_seconds / 3600.0, 1)

            items.append(
                {
                    "type": int(row.get("taskType") or 0),
                    "id": self._build_work_item_id(row.get("taskType"), row.get("requestId") or row.get("id")),
                    "title": row.get("taskName"),
                    "source": "Customer" if row.get("wasRequested") else "Employee",
                    "priority": int(row.get("priority") or 0),
                    "assignedSupervisor": row.get("assignedSupervisor"),
                    "assignedTeamLeader": row.get("assignedTeamLeader"),
                    "raised": self._fmt_datetime_short(start_at),
                    "resolved": self._fmt_datetime_short(resolved_at),
                    "resolveHrs": resolve_hrs,
                    "status": item_status,
                }
            )

        return items

    def _get_supervisor_performance(self, start_date, end_date):
        today = datetime.date.today()
        query = f'''
            SELECT
                COALESCE(t."supervisorId", '__UNASSIGNED__') AS "personKey",
                COALESCE(su."fullName", su.name, su."userName", t."supervisorId") AS "name",
                CASE
                    WHEN COUNT(DISTINCT COALESCE(tm."teamName", '-')) > 1 THEN 'Multiple'
                    ELSE COALESCE(MAX(tm."teamName"), '-')
                END AS "team",
                COUNT(*) FILTER (WHERE t."taskType" = {ISSUE}) AS "issuesAssigned",
                COUNT(*) FILTER (WHERE t."taskType" = {TASK}) AS "tasksAssigned",
                COUNT(*) FILTER (WHERE t."taskStatus" = {CLOSED}) AS "totalResolved",
                COUNT(*) FILTER (
                    WHERE t."taskStatus" <> {CLOSED}
                      AND t."dueDate" IS NOT NULL
                      AND t."dueDate"::date < %s
                ) AS "overdue",
                COALESCE(AVG(
                    CASE
                        WHEN t."taskStatus" = {CLOSED}
                         AND COALESCE(t."lastStatusDate", t."lastStatusDate") IS NOT NULL
                         AND COALESCE(t."createdAt", t."startDate") IS NOT NULL
                         AND COALESCE(t."lastStatusDate", t."lastStatusDate") >= COALESCE(t."createdAt", t."startDate")
                        THEN EXTRACT(EPOCH FROM (COALESCE(t."lastStatusDate", t."lastStatusDate") - COALESCE(t."createdAt", t."startDate"))) / 3600.0
                        ELSE NULL
                    END
                ), 0) AS "avgResolveHrs",
                COUNT(*) FILTER (WHERE t."taskType" = {ISSUE} AND COALESCE(t."wasRequested", FALSE) = TRUE) AS "customerRaisedIssues",
                COUNT(*) FILTER (
                    WHERE t."taskType" = {ISSUE}
                      AND COALESCE(t."wasRequested", FALSE) = TRUE
                      AND t."taskStatus" = {CLOSED}
                ) AS "customerResolvedIssues"
            FROM "taskManager" t
            LEFT JOIN "user" su
                ON su."employeeId" = t."supervisorId"
               AND COALESCE(su."isDeleted", 0) = 0
            LEFT JOIN "user" tl
                ON tl."employeeId" = t."teamLeaderId"
               AND COALESCE(tl."isDeleted", 0) = 0
            LEFT JOIN "teamManagement" tm
                ON tm."teamLeaderId" = tl.id
               AND COALESCE(tm."isDeleted", 0) = 0
            WHERE COALESCE(t."isDeleted", 0) = 0
              AND COALESCE(t."approvalStatus", 0) = 1
              AND t."taskType" IN ({TASK}, {ISSUE})
              AND t."startDate"::date BETWEEN %s AND %s
                        GROUP BY COALESCE(t."supervisorId", '__UNASSIGNED__'), COALESCE(su."fullName", su.name, su."userName", t."supervisorId")
            ORDER BY (COUNT(*) FILTER (WHERE t."taskType" = {ISSUE}) + COUNT(*) FILTER (WHERE t."taskType" = {TASK})) DESC,
                     "name" ASC
            LIMIT 100
            
        '''
        rows = execute_query(query, [today, start_date, end_date], many=True) or []

        result = []
        for row in rows:
            issues_assigned = int(row.get("issuesAssigned") or 0)
            tasks_assigned = int(row.get("tasksAssigned") or 0)
            total_assigned = issues_assigned + tasks_assigned
            total_resolved = int(row.get("totalResolved") or 0)
            overdue = int(row.get("overdue") or 0)
            completion_pct = round((total_resolved / total_assigned) * 100, 0) if total_assigned > 0 else 0
            monitor_status = 0 if completion_pct >= 85 and overdue <= 1 else 1

            result.append(
                {
                    "name": row.get("name"),
                    "team": row.get("team"),
                    "issuesAssigned": issues_assigned,
                    "tasksAssigned": tasks_assigned,
                    "totalResolved": total_resolved,
                    "overdue": overdue,
                    "completionPct": int(completion_pct),
                    "avgResolveHrs": self._safe_num(row.get("avgResolveHrs"), 1),
                    "customerRaisedIssues": int(row.get("customerRaisedIssues") or 0),
                    "customerResolvedIssues": int(row.get("customerResolvedIssues") or 0),
                    "status": monitor_status,
                }
            )

        return result

    def _get_team_leader_performance(self, start_date, end_date):
        today = datetime.date.today()
        query = f'''
            SELECT
                COALESCE(t."teamLeaderId", '__UNASSIGNED__') AS "personKey",
                COALESCE(tl."fullName", tl.name, tl."userName", t."teamLeaderId") AS "name",
                CASE
                    WHEN COUNT(DISTINCT COALESCE(tm."teamName", '-')) > 1 THEN 'Multiple'
                    ELSE COALESCE(MAX(tm."teamName"), '-')
                END AS "team",
                COUNT(*) FILTER (WHERE t."taskType" = {ISSUE}) AS "issuesAssigned",
                COUNT(*) FILTER (WHERE t."taskType" = {TASK}) AS "tasksAssigned",
                COUNT(*) FILTER (WHERE t."taskStatus" = {CLOSED}) AS "totalResolved",
                COUNT(*) FILTER (
                    WHERE t."taskStatus" <> {CLOSED}
                      AND t."dueDate" IS NOT NULL
                      AND t."dueDate"::date < %s
                ) AS "overdue",
                COALESCE(AVG(
                    CASE
                        WHEN t."taskStatus" = {CLOSED}
                         AND COALESCE(t."lastStatusDate", t."lastStatusDate") IS NOT NULL
                         AND COALESCE(t."createdAt", t."startDate") IS NOT NULL
                         AND COALESCE(t."lastStatusDate", t."lastStatusDate") >= COALESCE(t."createdAt", t."startDate")
                        THEN EXTRACT(EPOCH FROM (COALESCE(t."lastStatusDate", t."lastStatusDate") - COALESCE(t."createdAt", t."startDate"))) / 3600.0
                        ELSE NULL
                    END
                ), 0) AS "avgResolveHrs",
                COUNT(*) FILTER (WHERE t."taskType" = {ISSUE} AND COALESCE(t."wasRequested", FALSE) = TRUE) AS "customerRaisedIssues",
                COUNT(*) FILTER (
                    WHERE t."taskType" = {ISSUE}
                      AND COALESCE(t."wasRequested", FALSE) = TRUE
                      AND t."taskStatus" = {CLOSED}
                ) AS "customerResolvedIssues"
            FROM "taskManager" t
            LEFT JOIN "user" tl
                ON tl."employeeId" = t."teamLeaderId"
               AND COALESCE(tl."isDeleted", 0) = 0
            LEFT JOIN "teamManagement" tm
                ON tm."teamLeaderId" = tl.id
               AND COALESCE(tm."isDeleted", 0) = 0
            WHERE COALESCE(t."isDeleted", 0) = 0
              AND COALESCE(t."approvalStatus", 0) = 1
              AND t."taskType" IN ({TASK}, {ISSUE})
                            AND t."teamLeaderId" IS NOT NULL
                            AND t."teamLeaderId" <> ''
              AND t."startDate"::date BETWEEN %s AND %s
                        GROUP BY COALESCE(t."teamLeaderId", '__UNASSIGNED__'), COALESCE(tl."fullName", tl.name, tl."userName", t."teamLeaderId")
            ORDER BY (COUNT(*) FILTER (WHERE t."taskType" = {ISSUE}) + COUNT(*) FILTER (WHERE t."taskType" = {TASK})) DESC,
                     "name" ASC
            LIMIT 100
        '''
        rows = execute_query(query, [today, start_date, end_date], many=True) or []

        result = []
        for row in rows:
            issues_assigned = int(row.get("issuesAssigned") or 0)
            tasks_assigned = int(row.get("tasksAssigned") or 0)
            total_assigned = issues_assigned + tasks_assigned
            total_resolved = int(row.get("totalResolved") or 0)
            overdue = int(row.get("overdue") or 0)
            completion_pct = round((total_resolved / total_assigned) * 100, 0) if total_assigned > 0 else 0
            monitor_status = 0 if completion_pct >= 85 and overdue <= 1 else 1

            result.append(
                {
                    "name": row.get("name"),
                    "team": row.get("team"),
                    "issuesAssigned": issues_assigned,
                    "tasksAssigned": tasks_assigned,
                    "totalResolved": total_resolved,
                    "overdue": overdue,
                    "completionPct": int(completion_pct),
                    "avgResolveHrs": self._safe_num(row.get("avgResolveHrs"), 1),
                    "customerRaisedIssues": int(row.get("customerRaisedIssues") or 0),
                    "customerResolvedIssues": int(row.get("customerResolvedIssues") or 0),
                    "status": monitor_status,
                }
            )

        return result
import datetime
import json
import pytz
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, log_activity_raw
from api.constants import AMC_STATUS_ACTIVE, AMC_JOB_STATUS_COMPLETED


class AMCMaintenanceReportView(APIView):
    """
    Comprehensive AMC Maintenance Report API.
    
    Returns analytics data for the reporting dashboard including:
    - Summary cards (villas serviced, total hours, avg time, completion rate, etc.)
    - Team efficiency breakdown (garden vs pool)
    - Time per villa (bar chart data)
    - Split-schedule villas
    - Villa-by-villa breakdown table
    - Weekly time heatmap
    - Today's job timeline
    
    Query Parameters:
        startDate: YYYY-MM-DD (required)
        endDate: YYYY-MM-DD (required)
        visitType: 1 (Garden) | 2 (Pool) | empty (All)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            dubai_tz = pytz.timezone('Asia/Dubai')
            now_dubai = datetime.datetime.now(dubai_tz)

            # --- 1. Parse Query Parameters ---
            start_date_str = request.query_params.get("startDate", "").strip()
            end_date_str = request.query_params.get("endDate", "").strip()
            visit_type = request.query_params.get("visitType", "").strip()
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            heatmap_start_date_str = request.query_params.get("heatmapStartDate", "").strip()
            heatmap_end_date_str = request.query_params.get("heatmapEndDate", "").strip()
                

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
                    action="AMCMaintenanceReport",
                    performer=request.user,
                    details={
                        "startDate": start_date,
                        "endDate": end_date,
                        "visitType": visit_type or None,
                        "heatmapStartDate": heatmap_start_date_str or None,
                        "heatmapEndDate": heatmap_end_date_str or None,
                        "filtersUsed": dict(request.query_params),
                    },
                )
                update_report_time_query = """
                    UPDATE public."reportsViews"
                    SET "dateTime" = %s
                    WHERE id = 3
                """
                execute_query(update_report_time_query, [now_dubai], many=False)
                return success_response(
                    
                    message="Report export initiated. Download will be available shortly.", status_code=status.HTTP_200_OK
                )

            # Optional dedicated date range for heatmap. If not provided, fallback to main range.
            heatmap_start_date = start_date
            heatmap_end_date = end_date
            if heatmap_start_date_str or heatmap_end_date_str:
                if not heatmap_start_date_str or not heatmap_end_date_str:
                    return error_response(
                        message="Provide both heatmapStartDate and heatmapEndDate in YYYY-MM-DD format.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    heatmap_start_date = datetime.datetime.strptime(heatmap_start_date_str, "%Y-%m-%d").date()
                    heatmap_end_date = datetime.datetime.strptime(heatmap_end_date_str, "%Y-%m-%d").date()
                except ValueError:
                    return error_response(
                        message="Invalid heatmap date format. Use YYYY-MM-DD.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

                if heatmap_start_date > heatmap_end_date:
                    return error_response(
                        message="heatmapStartDate cannot be after heatmapEndDate.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

            # For comparison metrics (same duration immediately before the selected range)
            prev_start, prev_end = self._get_previous_range(start_date, end_date)

            # --- 2. Build visit type filter ---
            vt_filter = ""
            vt_params = []
            if visit_type in ("1", "2"):
                vt_filter = 'AND aj."visitType" = %s'
                vt_params = [int(visit_type)]

            # --- 3. Gather all analytics data ---
            summary = self._get_summary(
                start_date, end_date, prev_start, prev_end, vt_filter, vt_params
            )
            team_efficiency = self._get_team_efficiency(
                start_date, end_date, vt_filter, vt_params
            )
            time_per_villa = self._get_time_per_villa(
                start_date, end_date, vt_filter, vt_params
            )
            split_schedule_villas = self._get_split_schedule_villas(
                start_date, end_date
            )
            villa_breakdown = self._get_villa_breakdown(
                start_date, end_date, vt_filter, vt_params
            )
            weekly_heatmap = self._get_weekly_heatmap(
                heatmap_start_date,
                heatmap_end_date,
                vt_filter,
                vt_params,
            )
            today_timeline = self._get_today_timeline(start_date, end_date, vt_filter, vt_params)
            team_scoreboard = self._get_team_scoreboard(start_date, end_date, prev_start, prev_end)

            report_data = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "heatmapStartDate": heatmap_start_date.isoformat(),
                "heatmapEndDate": heatmap_end_date.isoformat(),
                "summary": summary,
                "teamEfficiency": team_efficiency,
                "timePerVilla": time_per_villa,
                "splitScheduleVillas": split_schedule_villas,
                "villaBreakdown": villa_breakdown,
                "weeklyHeatmap": weekly_heatmap,
                "todayTimeline": today_timeline,
                "teamScoreBoard": team_scoreboard,
            }

            return success_response(
                data=report_data,
                message="AMC Maintenance Report fetched successfully",
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return error_response(
                message=f"Error fetching AMC Maintenance Report: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ─────────────────────────── Date Helpers ────────────────────────────

    def _get_previous_range(self, start_date, end_date):
        delta = end_date - start_date + datetime.timedelta(days=1)
        prev_end = start_date - datetime.timedelta(days=1)
        prev_start = prev_end - delta + datetime.timedelta(days=1)
        return prev_start, prev_end

    # ─────────────────────── 1. Summary Cards ────────────────────────────

    def _get_summary(self, start_date, end_date, prev_start, prev_end, vt_filter, vt_params):
        """
        Returns:
        - villasServicedToday / totalScheduled
        - totalJobHours + comparison
        - avgTimePerVilla + comparison
        - completionRate + comparison
        - gardenJobsDone / gardenJobsTotal + carryOver
        - poolJobsDone / poolJobsTotal
        """
        # Current period stats
        current_query = f"""
            SELECT
                COUNT(DISTINCT CASE WHEN aj."visitStatus" != 0 THEN COALESCE(m."villaId", 0) END) AS "villasServiced",
                COUNT(DISTINCT COALESCE(m."villaId", 0)) AS "totalScheduled",
                COALESCE(SUM(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 3600.0
                        ELSE 0
                    END
                ), 0) AS "totalJobHours",
                COUNT(*) AS "totalJobs",
                COUNT(CASE WHEN aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END) AS "completedJobs",
                COUNT(CASE WHEN aj."visitType" = 1 THEN 1 END) AS "gardenJobsTotal",
                COUNT(CASE WHEN aj."visitType" = 1 AND aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END) AS "gardenJobsDone",
                COUNT(CASE WHEN aj."visitType" = 2 THEN 1 END) AS "poolJobsTotal",
                COUNT(CASE WHEN aj."visitType" = 2 AND aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END) AS "poolJobsDone"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            WHERE aj."visitDate" BETWEEN %s AND %s
            {vt_filter}
        """
        current_params = [start_date, end_date] + vt_params
        current = execute_query(current_query, current_params, many=False)
        if isinstance(current, list):
            current = current[0] if current else {}

        # Previous period stats for comparison
        prev_query = f"""
            SELECT
                COALESCE(SUM(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 3600.0
                        ELSE 0
                    END
                ), 0) AS "totalJobHours",
                COUNT(*) AS "totalJobs",
                COUNT(CASE WHEN aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END) AS "completedJobs"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            WHERE aj."visitDate" BETWEEN %s AND %s
            {vt_filter}
        """
        prev_params = [prev_start, prev_end] + vt_params
        prev = execute_query(prev_query, prev_params, many=False)
        if isinstance(prev, list):
            prev = prev[0] if prev else {}

        total_jobs = current.get("totalJobs", 0) or 0
        completed_jobs = current.get("completedJobs", 0) or 0
        total_hours = float(current.get("totalJobHours", 0) or 0)
        prev_total_hours = float(prev.get("totalJobHours", 0) or 0)
        prev_total_jobs = prev.get("totalJobs", 0) or 0
        prev_completed = prev.get("completedJobs", 0) or 0

        villas_serviced = current.get("villasServiced", 0) or 0
        total_scheduled = current.get("totalScheduled", 0) or 0

        # Avg time per villa in minutes
        avg_time_minutes = round((total_hours * 60) / villas_serviced, 0) if villas_serviced > 0 else 0
        prev_avg = round((prev_total_hours * 60) / (prev.get("totalJobs", 1) or 1), 0) if prev_total_hours > 0 else 0

        completion_rate = round((completed_jobs / total_jobs) * 100, 1) if total_jobs > 0 else 0
        prev_completion = round((prev_completed / prev_total_jobs) * 100, 1) if prev_total_jobs > 0 else 0

        garden_total = current.get("gardenJobsTotal", 0) or 0
        garden_done = current.get("gardenJobsDone", 0) or 0
        pool_total = current.get("poolJobsTotal", 0) or 0
        pool_done = current.get("poolJobsDone", 0) or 0

        return {
            "villasServicedToday": villas_serviced,
            "totalScheduled": total_scheduled,
            "totalJobHours": round(total_hours, 1),
            "totalJobHoursChange": round(total_hours - prev_total_hours, 1),
            "avgTimePerVilla": self._format_duration_hm(avg_time_minutes),
            "avgTimePerVillaMinutes": avg_time_minutes,
            "avgTimeChange": round(avg_time_minutes - prev_avg, 0),
            "completionRate": completion_rate,
            "completionRateChange": round(completion_rate - prev_completion, 1),
            "gardenJobsDone": garden_done,
            "gardenJobsTotal": garden_total,
            "gardenCarryOver": garden_total - garden_done,
            "poolJobsDone": pool_done,
            "poolJobsTotal": pool_total,
            "poolCarryOver": pool_total - pool_done,
        }

    # ─────────────────── 2. Team Efficiency ──────────────────────────────

    def _get_team_efficiency(self, start_date, end_date, vt_filter, vt_params):
        """
        Returns garden and pool team efficiency stats:
        - assigned, completed, avgDuration, totalTime, completionPct, carryOver, scheduleStatus
        """
        query = f"""
            SELECT
                aj."visitType",
                COUNT(*) AS "assigned",
                COUNT(CASE WHEN aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END) AS "completed",
                COALESCE(AVG(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0
                        ELSE NULL
                    END
                ), 0) AS "avgDurationMinutes",
                COALESCE(SUM(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 3600.0
                        ELSE 0
                    END
                ), 0) AS "totalTimeHours"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            WHERE aj."visitDate" BETWEEN %s AND %s
            {vt_filter}
            GROUP BY aj."visitType"
            ORDER BY aj."visitType"
        """
        params = [start_date, end_date] + vt_params
        rows = execute_query(query, params, many=True)
        if not rows:
            rows = []

        result = {"gardenTeam": None, "poolTeam": None}

        for row in rows:
            vt = row["visitType"]
            assigned = row["assigned"] or 0
            completed = row["completed"] or 0
            avg_minutes = float(row["avgDurationMinutes"] or 0)
            total_hours = float(row["totalTimeHours"] or 0)
            carry_over = assigned - completed
            pct = round((completed / assigned) * 100, 0) if assigned > 0 else 0

            if completed >= assigned and assigned > 0:
                schedule_status = "Early Finish" if completed > 0 else "On Schedule"
            elif pct >= 80:
                schedule_status = "On Schedule"
            else:
                schedule_status = "Behind Schedule"

            team_data = {
                "assigned": assigned,
                "completed": completed,
                "avgDuration": self._format_duration_hm(avg_minutes),
                "avgDurationMinutes": round(avg_minutes, 0),
                "totalTime": f"{round(total_hours, 1)}h",
                "totalTimeHours": round(total_hours, 1),
                "completionDone": completed,
                "completionTotal": assigned,
                "completionPct": pct,
                "carryOver": carry_over,
                "scheduleStatus": schedule_status,
            }

            if vt == 1:
                result["gardenTeam"] = team_data
            elif vt == 2:
                result["poolTeam"] = team_data

        return result

    # ─────────────────── 3. Time per Villa ───────────────────────────────

    def _get_time_per_villa(self, start_date, end_date, vt_filter, vt_params):
        """
        Returns garden + pool time breakdown per villa for bar chart.
        """
        query = f"""
            SELECT
                vd."villaName",
                m."villaId",
                aj."visitType",
                COALESCE(SUM(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0
                        ELSE 0
                    END
                ), 0) AS "totalMinutes",
                BOOL_OR(
                    CASE WHEN aj."visitType" = 1 THEN TRUE ELSE FALSE END
                ) OVER (PARTITION BY m."villaId") AS "hasGarden",
                BOOL_OR(
                    CASE WHEN aj."visitType" = 2 THEN TRUE ELSE FALSE END
                ) OVER (PARTITION BY m."villaId") AS "hasPool"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            LEFT JOIN "villaDetails" vd ON m."villaId" = vd.id AND COALESCE(vd."isDeleted", 0) = 0
            WHERE aj."visitDate" BETWEEN %s AND %s
            {vt_filter}
            GROUP BY vd."villaName", m."villaId", aj."visitType"
            ORDER BY vd."villaName"
        """
        params = [start_date, end_date] + vt_params
        rows = execute_query(query, params, many=True)
        if not rows:
            rows = []

        # Pivot: group by villa, split garden/pool
        villas = {}
        for row in rows:
            villa_id = row.get("villaId")
            villa_name = row.get("villaName") or f"Villa-{villa_id}"
            vt = row.get("visitType")
            minutes = round(float(row.get("totalMinutes", 0)), 0)

            if villa_id not in villas:
                villas[villa_id] = {
                    "villaId": villa_id,
                    "villaName": villa_name,
                    "gardenMinutes": 0,
                    "poolMinutes": 0,
                    "totalMinutes": 0,
                    "isSplitSchedule": False,
                }

            if vt == 1:
                villas[villa_id]["gardenMinutes"] = minutes
            elif vt == 2:
                villas[villa_id]["poolMinutes"] = minutes

            villas[villa_id]["totalMinutes"] = (
                villas[villa_id]["gardenMinutes"] + villas[villa_id]["poolMinutes"]
            )

        # Check split schedule (garden and pool on different days)
        split_query = f"""
            SELECT DISTINCT m."villaId"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            WHERE aj."visitDate" BETWEEN %s AND %s
            GROUP BY m."villaId", aj."visitDate"
            HAVING COUNT(DISTINCT aj."visitType") = 1
            INTERSECT
            SELECT m."villaId"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            WHERE aj."visitDate" BETWEEN %s AND %s
            GROUP BY m."villaId"
            HAVING COUNT(DISTINCT aj."visitType") > 1
        """
        split_params = [start_date, end_date, start_date, end_date]
        split_rows = execute_query(split_query, split_params, many=True)
        split_villa_ids = {r["villaId"] for r in (split_rows or [])}

        for vid, v in villas.items():
            v["isSplitSchedule"] = vid in split_villa_ids

        return sorted(villas.values(), key=lambda x: x.get("villaName") or "")

    # ─────────────────── 4. Split Schedule Villas ────────────────────────

    def _get_split_schedule_villas(self, start_date, end_date):
        """
        Villas where Garden & Pool maintenance fall on different days.
        """
        query = f"""
            WITH villa_visit_days AS (
                SELECT
                    m."villaId",
                    vd."villaName",
                    aj."visitType",
                    aj."visitDate",
                    COALESCE(
                        CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                            THEN ROUND(EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0)
                            ELSE NULL
                        END
                    , 0) AS "durationMinutes"
                FROM "AmcJobs" aj
                JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
                LEFT JOIN "villaDetails" vd ON m."villaId" = vd.id AND COALESCE(vd."isDeleted", 0) = 0
                WHERE aj."visitDate" BETWEEN %s AND %s
            ),
            split_villas AS (
                SELECT "villaId"
                FROM villa_visit_days
                GROUP BY "villaId"
                HAVING COUNT(DISTINCT "visitType") > 1
                   AND COUNT(DISTINCT CASE WHEN "visitType" = 1 THEN "visitDate" END) > 0
                   AND COUNT(DISTINCT CASE WHEN "visitType" = 2 THEN "visitDate" END) > 0
                   AND NOT EXISTS (
                       SELECT 1 FROM villa_visit_days vvd2
                       WHERE vvd2."villaId" = villa_visit_days."villaId"
                       GROUP BY vvd2."visitDate"
                       HAVING COUNT(DISTINCT vvd2."visitType") > 1
                   )
            )
            SELECT
                vvd."villaId",
                vvd."villaName",
                vvd."visitType",
                vvd."visitDate",
                vvd."durationMinutes"
            FROM villa_visit_days vvd
            JOIN split_villas sv ON vvd."villaId" = sv."villaId"
            ORDER BY vvd."villaName", vvd."visitType", vvd."visitDate" DESC
        """
        rows = execute_query(query, [start_date, end_date], many=True)
        if not rows:
            return []

        # Group by villa
        villas = {}
        for row in rows:
            vid = row["villaId"]
            if vid not in villas:
                villas[vid] = {
                    "villaId": vid,
                    "villaName": row["villaName"] or f"Villa-{vid}",
                    "garden": None,
                    "pool": None,
                }
            vt = row["visitType"]
            entry = {
                "visitDate": row["visitDate"].isoformat() if hasattr(row["visitDate"], "isoformat") else str(row["visitDate"]),
                "durationMinutes": int(row["durationMinutes"] or 0),
            }
            # Take the most recent visit for each type
            if vt == 1 and villas[vid]["garden"] is None:
                villas[vid]["garden"] = entry
            elif vt == 2 and villas[vid]["pool"] is None:
                villas[vid]["pool"] = entry

        return list(villas.values())

    # ─────────────────── 5. Villa Breakdown Table ────────────────────────

    def _get_villa_breakdown(self, start_date, end_date, vt_filter, vt_params):
        """
        Villa-by-villa table with schedule times, durations, total time, trend, status.
        """
        query = f"""
            WITH villa_jobs AS (
                SELECT
                    m."villaId",
                    vd."villaName",
                    vd."community" AS "zone",
                    aj."amcJobId",
                    aj."visitType",
                    aj."visitDate",
                    aj."visitStatus",
                    aj."startTime",
                    aj."endTime",
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN ROUND(EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0)
                        ELSE 0
                    END AS "durationMinutes"
                FROM "AmcJobs" aj
                JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
                LEFT JOIN "villaDetails" vd ON m."villaId" = vd.id AND COALESCE(vd."isDeleted", 0) = 0
                WHERE aj."visitDate" BETWEEN %s AND %s
                {vt_filter}
            )
            SELECT
                "villaId",
                "villaName",
                "zone",
                "amcJobId",
                "visitType",
                "visitDate",
                "visitStatus",
                "startTime"::text,
                "endTime"::text,
                "durationMinutes"
            FROM villa_jobs
            ORDER BY "villaName", "visitDate", "visitType"
        """
        params = [start_date, end_date] + vt_params
        rows = execute_query(query, params, many=True)
        if not rows:
            rows = []

        # Get last visit totals before the current range for trend comparison.
        # This uses the most recent previous visit date for each villa, not a fixed previous-day window.
        prev_query = f"""
            WITH last_visit AS (
                SELECT
                    m."villaId",
                    MAX(aj."visitDate") AS "lastVisitDate"
                FROM "AmcJobs" aj
                JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
                WHERE aj."visitDate" < %s
                {vt_filter}
                GROUP BY m."villaId"
            )
            SELECT
                m."villaId",
                COALESCE(SUM(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0
                        ELSE 0
                    END
                ), 0) AS "prevTotalMinutes"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            JOIN last_visit lv ON lv."villaId" = m."villaId" AND aj."visitDate" = lv."lastVisitDate"
            WHERE aj."visitDate" < %s
            {vt_filter}
            GROUP BY m."villaId"
        """
        prev_params = [start_date, start_date] + vt_params + vt_params
        prev_rows = execute_query(prev_query, prev_params, many=True)
        prev_map = {r["villaId"]: float(r["prevTotalMinutes"] or 0) for r in (prev_rows or [])}

        # Group by villa
        villas = {}
        for row in rows:
            vid = row["villaId"]
            if vid not in villas:
                villas[vid] = {
                    "villaId": vid,
                    "villaName": row["villaName"] or f"Villa-{vid}",
                    "zone": row["zone"] or "",
                    "jobs": [],
                    "totalMinutes": 0,
                }

            duration = float(row["durationMinutes"] or 0)
            villas[vid]["totalMinutes"] += duration
            villas[vid]["jobs"].append({
                "amcJobId": row["amcJobId"],
                "visitType": row["visitType"],
                "visitTypeName": "Garden" if row["visitType"] == 1 else "Pool",
                "visitDate": row["visitDate"].isoformat() if hasattr(row["visitDate"], "isoformat") else str(row["visitDate"]),
                "visitStatus": row["visitStatus"],
                "startTime": row["startTime"],
                "endTime": row["endTime"],
                "durationMinutes": round(duration),
            })

        result = []
        for vid, v in villas.items():
            total_min = round(v["totalMinutes"])
            prev_min = round(prev_map.get(vid, 0))
            trend_change = round(total_min - prev_min)

            # Determine status
            all_completed = all(j["visitStatus"] == AMC_JOB_STATUS_COMPLETED for j in v["jobs"])
            any_started = any(j["visitStatus"] != 0 for j in v["jobs"])
            today = datetime.date.today()
            has_today_jobs = any(j["visitDate"] == today.isoformat() for j in v["jobs"])

            if all_completed:
                villa_status = "Completed"
            elif not has_today_jobs:
                villa_status = "Not Today"
            elif any_started:
                villa_status = "On Track"
            else:
                villa_status = "Scheduled"

            result.append({
                "villaId": vid,
                "villaName": v["villaName"],
                "zone": v["zone"],
                "jobs": v["jobs"],
                "totalMinutes": total_min,
                "totalTime": self._format_duration_hm(total_min),
                "previousTotalMinutes": prev_min,
                "trendChange": trend_change,
                "status": villa_status,
                "isSplitDays": len(set(j["visitType"] for j in v["jobs"])) > 1 and len(set(j["visitDate"] for j in v["jobs"])) > 1,
            })

        result.sort(key=lambda x: x.get("villaName") or "")
        return result

    # ─────────────────── 6. Weekly Heatmap ───────────────────────────────

    def _get_weekly_heatmap(self, start_date, end_date, vt_filter, vt_params):
        """
        Total minutes per villa per day-of-week within the given date range.
        Returns: { villas: [...], days: ['SUN','MON',...], data: {villaName: {day: minutes}} }
        """
        query = f"""
            SELECT
                vd."villaName",
                m."villaId",
                aj."visitDate",
                EXTRACT(DOW FROM aj."visitDate") AS "dayOfWeek",
                COALESCE(SUM(
                    CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0
                        ELSE 0
                    END
                ), 0) AS "totalMinutes"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            LEFT JOIN "villaDetails" vd ON m."villaId" = vd.id AND COALESCE(vd."isDeleted", 0) = 0
            WHERE aj."visitDate" BETWEEN %s AND %s
            {vt_filter}
            GROUP BY vd."villaName", m."villaId", aj."visitDate", EXTRACT(DOW FROM aj."visitDate")
            ORDER BY vd."villaName", aj."visitDate"
        """
        params = [start_date, end_date] + vt_params
        rows = execute_query(query, params, many=True)
        if not rows:
            rows = []

        day_names = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
        heatmap_data = {}
        villa_set = set()

        for row in rows:
            villa_name = row.get("villaName") or f"Villa-{row.get('villaId')}"
            # PostgreSQL DOW: Sunday=0, Monday=1, ..., Saturday=6
            dow = int(row.get("dayOfWeek", 0))
            minutes = round(float(row.get("totalMinutes", 0)))

            villa_set.add(villa_name)
            if villa_name not in heatmap_data:
                heatmap_data[villa_name] = {}

            day_label = day_names[dow]
            heatmap_data[villa_name][day_label] = minutes

        villas = sorted(villa_set)

        return {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "days": day_names,
            "villas": villas,
            "data": heatmap_data,
        }

    # ─────────────────── 7. Today's Job Timeline ─────────────────────────

    def _get_today_timeline(self, start_date, end_date, vt_filter, vt_params):
        """
        Chronological job log for target_date with start/end times.
        """
        query = f"""
            SELECT
                aj."amcJobId",
                vd."villaName",
                m."villaId",
                aj."visitType",
                aj."visitDate",
                aj."startTime"::text AS "startTime",
                aj."endTime"::text AS "endTime",
                aj."visitStatus",
                CASE WHEN aj."startTime" IS NOT NULL AND aj."endTime" IS NOT NULL
                    THEN ROUND(EXTRACT(EPOCH FROM (aj."endTime" - aj."startTime")) / 60.0)
                    ELSE NULL
                END AS "durationMinutes",
                CASE WHEN aj."visitType" = 1
                    THEN COALESCE(gs."fullName", gs.name, gs."userName")
                    ELSE COALESCE(ps."fullName", ps.name, ps."userName")
                END AS "supervisorName",
                aj."visitType" AS "teamType"
            FROM "AmcJobs" aj
            JOIN "AMCMaster" m ON aj."amcId" = m."amcId" AND m."isDeleted" = 0 AND m."status" = {AMC_STATUS_ACTIVE}
            LEFT JOIN "villaDetails" vd ON m."villaId" = vd.id AND COALESCE(vd."isDeleted", 0) = 0
            LEFT JOIN "user" gs ON m."gardenSupervisorId" = gs."employeeId" AND COALESCE(gs."isDeleted", 0) = 0
            LEFT JOIN "user" ps ON m."poolSupervisorId" = ps."employeeId" AND COALESCE(ps."isDeleted", 0) = 0
            WHERE aj."visitDate" BETWEEN %s AND %s
            AND aj."startTime" IS NOT NULL
            {vt_filter}
            ORDER BY aj."startTime" ASC
        """
        params = [start_date, end_date] + vt_params
        rows = execute_query(query, params, many=True)
        if not rows:
            rows = []

        timeline = []
        for row in rows:
            timeline.append({
                "amcJobId": row["amcJobId"],
                "villaName": row["villaName"] or f"Villa-{row['villaId']}",
                "villaId": row["villaId"],
                "visitType": row["visitType"],
                "visitDate": row["visitDate"],
                "visitTypeName": "Garden" if row["visitType"] == 1 else "Pool",
                "startTime": row["startTime"],
                "endTime": row["endTime"],
                "durationMinutes": int(row["durationMinutes"]) if row["durationMinutes"] is not None else None,
                "visitStatus": row["visitStatus"],
                "supervisorName": row["supervisorName"],
            })

        return timeline

    # ─────────────────── 8. Team Scoreboard ─────────────────────────────

    def _get_team_scoreboard(self, start_date, end_date, prev_start, prev_end):
        """
        Team-level scoreboard split by Garden / Pool section.
        Score = 50% job completion + 30% customer rating (0-5 → 0-30) + 20% supervisor rating (0-5 → 0-20)
        """

        def _section_period_label(sd, ed):
            delta = (ed - sd).days
            if delta == 6:
                return f"Week of {sd.strftime('%b')} {sd.day}-{ed.day}, {ed.year}"
            return f"{sd.strftime('%b')} {sd.day} \u2013 {ed.strftime('%b')} {ed.day}, {ed.year}"

        def _compute_score(assigned, completed, cust_rating, sup_rating):
            completion_pct = (completed / assigned) if assigned > 0 else 0
            cust_n = (float(cust_rating) / 5.0) if cust_rating else 0
            sup_n = (float(sup_rating) / 5.0) if sup_rating else 0
            return round(completion_pct * 50 + cust_n * 30 + sup_n * 20, 1)

        def _fetch_teams(visit_type, tl_col, date_from, date_to):
            query = f"""
                SELECT
                    tm.id                                                          AS "teamId",
                    tm."teamName",
                    COALESCE(u."fullName", u."name", u."userName")                 AS "teamLeaderName",
                    COUNT(aj."amcJobId")                                            AS "assigned",
                    COUNT(CASE WHEN aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END)
                                                                                   AS "completed",
                    ROUND(COALESCE(AVG(f_c."rating"), 0)::numeric, 2)             AS "customerRating",
                    ROUND(COALESCE(AVG(f_s."rating"), 0)::numeric, 2)             AS "supervisorRating",
                    COUNT(DISTINCT CASE
                        WHEN f_c."rating" IS NOT NULL OR f_s."rating" IS NOT NULL
                        THEN aj."amcJobId" END)                                    AS "ratingCount"
                FROM "teamManagement" tm
                JOIN "user" u
                    ON  tm."teamLeaderId" = u.id
                    AND COALESCE(u."isDeleted", 0) = 0
                JOIN "AMCMaster" m
                    ON  u."employeeId" = m."{tl_col}"
                    AND m."isDeleted" = 0
                    AND m."status"    = {AMC_STATUS_ACTIVE}
                JOIN "AmcJobs" aj
                    ON  aj."amcId"     = m."amcId"
                    AND aj."visitType" = {visit_type}
                    AND aj."visitDate" BETWEEN %s AND %s
                LEFT JOIN "AmcFeedback" f_c
                    ON  f_c."amcJobId"        = aj."amcJobId"
                    AND f_c."submittedByType" = 'customer'
                    AND f_c."isDeleted"       = 0
                LEFT JOIN "AmcFeedback" f_s
                    ON  f_s."amcJobId"        = aj."amcJobId"
                    AND f_s."submittedByType" = 'supervisor'
                    AND f_s."isDeleted"       = 0
                WHERE tm."isDeleted" = 0
                GROUP BY tm.id, tm."teamName", u."fullName", u."name", u."userName"
                ORDER BY tm."teamName"
            """
            return execute_query(query, [date_from, date_to], many=True) or []

        def _fetch_prev_scores(visit_type, tl_col, date_from, date_to):
            query = f"""
                SELECT
                    tm.id AS "teamId",
                    COUNT(aj."amcJobId") AS "assigned",
                    COUNT(CASE WHEN aj."visitStatus" = {AMC_JOB_STATUS_COMPLETED} THEN 1 END) AS "completed",
                    ROUND(COALESCE(AVG(f_c."rating"), 0)::numeric, 2) AS "customerRating",
                    ROUND(COALESCE(AVG(f_s."rating"), 0)::numeric, 2) AS "supervisorRating"
                FROM "teamManagement" tm
                JOIN "user" u
                    ON  tm."teamLeaderId" = u.id
                    AND COALESCE(u."isDeleted", 0) = 0
                JOIN "AMCMaster" m
                    ON  u."employeeId" = m."{tl_col}"
                    AND m."isDeleted" = 0
                    AND m."status"    = {AMC_STATUS_ACTIVE}
                JOIN "AmcJobs" aj
                    ON  aj."amcId"     = m."amcId"
                    AND aj."visitType" = {visit_type}
                    AND aj."visitDate" BETWEEN %s AND %s
                LEFT JOIN "AmcFeedback" f_c
                    ON  f_c."amcJobId"        = aj."amcJobId"
                    AND f_c."submittedByType" = 'customer'
                    AND f_c."isDeleted"       = 0
                LEFT JOIN "AmcFeedback" f_s
                    ON  f_s."amcJobId"        = aj."amcJobId"
                    AND f_s."submittedByType" = 'supervisor'
                    AND f_s."isDeleted"       = 0
                WHERE tm."isDeleted" = 0
                GROUP BY tm.id
            """
            rows = execute_query(query, [date_from, date_to], many=True) or []
            return {r["teamId"]: r for r in rows}

        period_label = _section_period_label(start_date, end_date)

        sections = []
        for section_key, section_title, visit_type, tl_col, team_prefix in [
            ("garden", "Garden Section", 1, "gardenTeamLeaderId", "G"),
            ("pool",   "Pool Section",   2, "poolTeamLeaderId",   "P"),
        ]:
            current_rows = _fetch_teams(visit_type, tl_col, start_date, end_date)
            prev_map     = _fetch_prev_scores(visit_type, tl_col, prev_start, prev_end)

            teams = []
            for idx, row in enumerate(current_rows, start=1):
                team_id      = row["teamId"]
                assigned     = row["assigned"] or 0
                completed    = row["completed"] or 0
                cust_rating  = float(row["customerRating"] or 0)
                sup_rating   = float(row["supervisorRating"] or 0)
                rating_count = row["ratingCount"] or 0

                team_score = _compute_score(assigned, completed, cust_rating, sup_rating)

                prev = prev_map.get(team_id, {})
                prev_score = _compute_score(
                    prev.get("assigned") or 0,
                    prev.get("completed") or 0,
                    float(prev.get("customerRating") or 0),
                    float(prev.get("supervisorRating") or 0),
                )

                teams.append({
                    "teamId":           team_id,
                    "teamCode":         f"{team_prefix}-{idx}",
                    "teamName":         row["teamName"],
                    "teamLeaderName":   row["teamLeaderName"],
                    "assigned":         assigned,
                    "completed":        completed,
                    "customerRating":   cust_rating,
                    "supervisorRating": sup_rating,
                    "ratingCount":      rating_count,
                    "teamScore":        team_score,
                    "previousScore":    prev_score,
                })

            sections.append({
                "sectionKey":    section_key,
                "sectionTitle":  section_title,
                "sectionPeriod": period_label,
                "teams":         teams,
            })

        return {"scoreSections": sections}

    # ─────────────────── Formatting Helpers ──────────────────────────────

    @staticmethod
    def _format_duration_hm(total_minutes):
        """Format minutes as 'Xh Ym' string."""
        total_minutes = int(total_minutes)
        if total_minutes <= 0:
            return "0m"
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h"
        else:
            return f"{minutes}m"

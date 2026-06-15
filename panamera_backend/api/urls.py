from django.urls import path
from .views import *
from .views.team_management import TeamManagementView, AvailableMembersView
from .views.team_leader_list import TeamLeaderListView
from .views.emergency_media_upload_view import EmergencyMediaUploadView
from .views.emergency_request_view import EmergencyRequestView
from .views.emergency_request_view import EmergencyRequestStatsView
from .views.amc_maintenance_report import AMCMaintenanceReportView
from .views.task_issue_report import TaskIssueReportView
from .views.material_request_view import MaterialRequestView
from .views.stock_material_view import StockMaterialView

urlpatterns = [
    path("login", LoginView.as_view(), name="login"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("forgotPassword", ForgotPasswordView.as_view(), name="forgot_password"),
    path("customerForgotPassword", CustomerForgotPasswordView.as_view(), name="customer_forgot_password"),
    path("refresh", CustomTokenRefreshView.as_view(), name="token_refresh"),
    path('clockIn', CheckInView.as_view(), name='api_attendance_check_in'),
    path('clockOut', CheckOutView.as_view(), name='api_attendance_check_out'),
    path('emergencyClockIn', EmergencyCheckInView.as_view(), name='emergency_check_in'),
    path('emergencyClockOut', EmergencyCheckOutView.as_view(), name='emergency_check_out'),
    path('emergencyCheckInOutStatus', EmergencyCheckinCheckoutStatus.as_view(), name='emergency_checkin_checkout_status'),
    path('emergencyMediaUpload', EmergencyMediaUploadView.as_view(), name='emergency_media_upload'),
    path('emergencyRequest', EmergencyRequestView.as_view(), name='emergency_request_create'),
    path('emergencyRequest/stats', EmergencyRequestStatsView.as_view(), name='emergency_request_stats'),
    path('emergencyRequest/<str:request_id>', EmergencyRequestView.as_view(), name='emergency_request_detail'),
    path("userRole", UserRoleList.as_view(), name="job_list"),
    path("employee", EmployeeView.as_view(), name="employee_list"),
    path("employee/<int:pk>", EmployeeView.as_view(), name="employee_detail"),
    path("clockInOutStatus", ClockInOutStatusView.as_view(), name="clock_in_out_status"),
    path("attendanceList", AttendanceListView.as_view(), name="attendance_list"),
    path("employeeByOrderRoleList", EmployeeByOrderRoleListView.as_view(), name="employeeByOrderRoleList"),
    path("skills", SkillListView.as_view(), name="skill_list"),
    path("allEmployeeList", AllEmployeeListView.as_view(), name="allEmployeeList"),
    path("attendance", AddAttendanceView.as_view(), name="clock_in_clock_out"),
    path("shift", ShiftView.as_view(), name="shift_list"),
    path("shift/<int:pk>", ShiftView.as_view(), name="shift_detail"),
    path("shiftMap", ShiftMapView.as_view(), name="shift_map"),
    path('userCredentials/<int:user_id>/', UserCredentialsView.as_view(), name='user_credentials'),
    path("updateOvertimeStatus", UpdateOvertimeStatusView.as_view(), name="update_overtime_status"),
    path("uploadOvertimeDetails", UploadOvertimeMediaView.as_view(), name="upload_overtime_media"),
    path("updateAttendanceStatus", UpdateAttendanceStatusView.as_view(), name="update_attendance_status"),
    path("functionalityList", FunctionalityListView.as_view(), name="functionality_list"),
    path("breakIn", BreakInView.as_view(), name="break_in"),
    path("breakOut", BreakOutView.as_view(), name="break_out"),
    path("earlyLeaveReason", UpdateEarlyReasonView.as_view(), name="update_early_reason"),
    path("storeFCMToken", StoreFCMTokenView.as_view(), name="store_fcm_token"),
    path("customerStoreFCMToken", CustomerStoreFCMTokenView.as_view(), name="customer_store_fcm_token"),
    path("absentList", GetAbsentAttendanceView.as_view(), name="absent_attendance"),
    path("updateEarlyReasonStatus", UpdateEarlyReasonStatusView.as_view(), name="update_early_reason_status"),
    path("notifications", NotificationListView.as_view(), name="notification_list"),
    path("markNotificationsRead", MarkNotificationsReadView.as_view(), name="mark_notifications_read"),
    path("activityLogs", ActivityLogsListAPI.as_view(), name="activity_logs_list"),
    path("customer", CustomerView.as_view(), name="customer_list"),
    path("customer/<int:customer_id>", CustomerView.as_view(), name="customer_detail"),
    path("amcMaster", AMCMasterView.as_view(), name="amc_list"),
    path("amcMaster/<int:amc_id>", AMCMasterView.as_view(), name="amc_detail"),
    path("allCustomers", CustomerListView.as_view(), name="all_customers_list"),
    path("amcJobs", AMCDailyJobsView.as_view(), name="amc_daily_jobs"),
    path("amcJobs/single/<int:amc_job_id>/", AMCDailyJobsView.as_view(), name="amc_daily_jobs_detail"),
    path("amcJobDetails/<int:amc_job_id>/", AMCJobDetailsView.as_view(), name="amc_job_details"),
    path("userHierarchy/<int:user_id>", UserHierarchyView.as_view(), name="user_hierarchy"),
    path("amcJobs/<int:amc_job_id>", AMCJobStatusUpdateView.as_view(), name="amc_job_status_update"),
    path("amcJobTaskUpdate/<int:visit_task_id>", AmcJobTaskUpdateView.as_view(), name="amc_job_task_update"),
    path("amcJobsCalendar", AMCProjectedCalendarView.as_view(), name="amc_projected_calendar"),
    path("amcComments/<int:amcJobId>", JobDayCommentView.as_view(), name="job_day_comment"),
    path("deleteCommentImages/<int:amcJobId>", JobDayCommentView.as_view(), name="delete_comment_images"),
    path("amcIssues/<int:amcJobId>", JobDayIssueView.as_view(), name="job_day_issue"),
    path("customerLogin", CustomerLoginView.as_view(), name="customer_login"),
    path("customerLogout", CustomerLogoutView.as_view(), name="customer_logout"),
    path("customerResetPassword/<int:customer_id>", CustomerResetPasswordView.as_view(), name="customer_reset_password"),
    path("taskManager", TaskManagerView.as_view(), name="task_manager"),
    path("taskManager/<int:task_id>", TaskManagerView.as_view(), name="task_manager_detail"),
    path("taskStatusUpdate/<int:taskk_id>", TaskStatusUpdateView.as_view(), name="task_status_update"),
    path("taskCommentsIssues", TaskCommentIssuesAttachmentView.as_view(), name="task_comments_issues"),
    path("taskCommentsIssues/<int:attachment_id>", TaskCommentIssuesAttachmentView.as_view(), name="task_comments_issues_detail"),
    path("mailTemplates", MailTemplateView.as_view(), name="mail_template_list"),
    path("mailTemplates/<int:template_id>", MailTemplateView.as_view(), name="mail_template_detail"),
    path("countOfJobStats", SupervisorJobStatsView.as_view(), name="supervisor_job_stats"),
    path("emailSettings", EmailSettingsView.as_view(), name="email_settings_detail"),
    path("projectImages", ProjectImagesView.as_view(), name="project_images"),
    path("projectImages/<int:image_id>", ProjectImagesView.as_view(), name="project_image_detail"),
    path("environmentURLs", EnvironmentURLView.as_view(), name="environment_urls"),
    path("feedback", FeedbackView.as_view(), name="feedback_list"),
    path("feedback/<int:feedback_id>", FeedbackView.as_view(), name="feedback_detail"),
    path("dashboard", DashboardView.as_view(), name="dashboard"),
    path("dashboardGraphs", DashboardGraphsView.as_view(), name="dashboard_graphs"),
    path("uploadAppVersion", UploadAppVersionView.as_view(), name="upload_app_version"),
    path("checkAppVersion", CheckAppVersionView.as_view(), name="check_app_version"),
    path("communityList", CommunityListView.as_view(), name="community_list"),
    path("monthlyAttendanceReport", MonthlyAttendanceReportView.as_view(), name="monthly_attendance_report"),
    path("reportViews", ReportViewsDateTimeAPIView.as_view(), name="report_views_datetime"),
    
    # Team Management - CRUD Operations
    path("teamMembers", TeamManagementView.as_view(), name="teams_list_create"),
    path("teamMembers/<int:team_id>", TeamManagementView.as_view(), name="team_detail_update_delete"),
    path("availableMembers", AvailableMembersView.as_view(), name="available_members_list"),
    path("teamLeaders", TeamLeaderListView.as_view(), name="team_leaders_list"),
    path("leaveApplications", LeaveApplicationView.as_view(), name="leave_application_list_create"),
    path("leaveApplications/<int:leave_id>", LeaveApplicationView.as_view(), name="leave_application_detail_update"),
    # path("userrole", RoleListView.as_view(), name="user_role_list"),
    path("nocExpiry", NOCExpiryView.as_view(), name="noc_expiry_data"),

    # AMC Maintenance Report — analytics dashboard
    path("amcMaintenanceReport", AMCMaintenanceReportView.as_view(), name="amc_maintenance_report"),

    # Task & Issue Report — analytics dashboard
    path("taskIssueReport", TaskIssueReportView.as_view(), name="task_issue_report"),

    # AMC Feedback — admin/supervisor view
    path("amcFeedback", AmcFeedbackView.as_view(), name="amc_feedback_list_create"),
    path("amcFeedback/<int:feedback_id>", AmcFeedbackView.as_view(), name="amc_feedback_detail"),

    # AMC Feedback — customer & supervisor submission (combined auth)
    path("customerAmcFeedback", CustomerAmcFeedbackView.as_view(), name="customer_amc_feedback_list_create"),
    path("customerAmcFeedback/<int:feedback_id>", CustomerAmcFeedbackView.as_view(), name="customer_amc_feedback_detail"),
    path("materialRequest", MaterialRequestView.as_view(), name="material_request_list_create"),
    path("materialRequest/<int:request_id>", MaterialRequestView.as_view(), name="material_request_detail_update_delete"),
    path("paymentKeyPassword", PaymentKeyPasswordView.as_view(), name="payment_key_password"),

    # Stock Material — CRUD
    path("stockMaterial", StockMaterialView.as_view(), name="stock_material_list_create"),
    path("stockMaterial/<int:stock_id>", StockMaterialView.as_view(), name="stock_material_detail_update_delete"),
]

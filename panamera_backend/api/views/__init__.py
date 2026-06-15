# from messages import *
# This file makes the views directory a Python package.
# Import all views here for easy access in urls.py
from .login_view import LoginView
from .logout_view import LogoutView
from .forgot_password_view import ForgotPasswordView, CustomerForgotPasswordView
from .token_refresh_view import CustomTokenRefreshView
from .check_in_view import CheckInView
from .check_out_view import CheckOutView
from .emergency_checkin_out_view import EmergencyCheckInView, EmergencyCheckOutView, EmergencyCheckinCheckoutStatus
from .userrole_list_view import UserRoleList
from .employee_view import EmployeeView
from .attendance_list_view import AttendanceListView
from .clock_in_out_status_view import ClockInOutStatusView
from .supervisor_list_view import EmployeeByOrderRoleListView
from .skill_list_view import SkillListView
from .attendancecrud import AddAttendanceView
from .shift_crud import ShiftView
from .shift_maping import ShiftMapView
from .user_credentials import UserCredentialsView
from .overtime_update_status import UpdateOvertimeStatusView
from .overtime_details_data import UploadOvertimeMediaView
from .update_attendance_status import UpdateAttendanceStatusView
from .functionality_list import FunctionalityListView
from .break_in import BreakInView
from .break_out import BreakOutView
from .early_leave_reason import UpdateEarlyReasonView
from .store_fcm_token import StoreFCMTokenView, CustomerStoreFCMTokenView
from .absent_list import GetAbsentAttendanceView
from .early_leave_update_status import UpdateEarlyReasonStatusView
from .notifications import NotificationListView, MarkNotificationsReadView
from .activitylogs import ActivityLogsListAPI
from .all_employees_list import AllEmployeeListView
from .customer_master import CustomerView
from .AMC_master import AMCMasterView
from .all_customer_list import CustomerListView
from .AMC_jobs import AMCDailyJobsView, AMCJobDetailsView, AMCJobStatusUpdateView, AmcJobTaskUpdateView, AMCProjectedCalendarView
from .user_hierarchychain import UserHierarchyView
from .Amc_job_comments import JobDayCommentView
from .Amc_job_issues import JobDayIssueView
from .customer_login import CustomerLoginView
from .customer_logout import CustomerLogoutView
from .customer_master import CustomerResetPasswordView
from .task_manager import TaskManagerView
from .task_status_update import TaskStatusUpdateView
from .task_comments_issues import TaskCommentIssuesAttachmentView
from .email_templete import MailTemplateView
from .job_task_issue_statscount import SupervisorJobStatsView
from .smtp_email_sender import EmailSettingsView
from .project_images import ProjectImagesView
from .url_redirection import EnvironmentURLView
from .feedback import FeedbackView
from .emergency_request_view import EmergencyRequestView, EmergencyRequestStatsView
from .dashboard_with_graphs import DashboardGraphsView, DashboardView
from .version_upload_check import CheckAppVersionView, UploadAppVersionView
from .community_list import CommunityListView
from .monthly_attendance_report import MonthlyAttendanceReportView
from .report_views_datetime import ReportViewsDateTimeAPIView
from .team_management import TeamManagementView
from .team_leader_list import TeamLeaderListView
from .emergency_media_upload_view import EmergencyMediaUploadView
from .leave_applications import LeaveApplicationView
from .noc_expiry_data import NOCExpiryView
from .amc_feedback import AmcFeedbackView, CustomerAmcFeedbackView
from .amc_maintenance_report import AMCMaintenanceReportView
from .task_issue_report import TaskIssueReportView
from .payment_key_password import PaymentKeyPasswordView
from .material_request_view import MaterialRequestView
from .stock_material_view import StockMaterialView

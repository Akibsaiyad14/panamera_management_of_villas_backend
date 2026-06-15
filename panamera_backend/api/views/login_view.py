from api.messages import *
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework import status
from django.contrib.auth.models import update_last_login
from django.conf import settings
from django.contrib.auth import get_user_model
import base64
from api.utils import success_response, error_response, log_activity_raw
from django.utils.timezone import now

User = get_user_model()  # Get your custom user model

class LoginView(APIView):
    def post(self, request):
        userName = request.data.get("userName")
        input_password = request.data.get("password")

        if not userName or not input_password:
            return error_response(message=USERNAME_AND_PASSWORD_REQUIRED, status_code=status.HTTP_400_BAD_REQUEST)

        userName = userName.strip().lower()
        

        try:
            user = User.objects.select_related('roleId').get(userName__iexact=userName)
        except User.DoesNotExist:
            return error_response(message=UNABLE_TO_LOGIN_WITH_CREDENTIALS, status_code=status.HTTP_401_UNAUTHORIZED)

        
        if user.employmentStatus != 'Active':
            return error_response(message=EMPLOYMENT_STATUS_NOT_ACTIVE, status_code=status.HTTP_401_UNAUTHORIZED)


        if user.isDeleted == 1:
            return error_response(message=UNABLE_TO_LOGIN_WITH_CREDENTIALS, status_code=status.HTTP_401_UNAUTHORIZED)

        

        try:
            stored_decoded_password = base64.b64decode(user.password.encode()).decode()
        except Exception:
            return error_response(message=SOMETHING_WENT_WRONG_PLEASE_TRY_AGAIN, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if stored_decoded_password != input_password:
            return error_response(message=UNABLE_TO_LOGIN_WITH_CREDENTIALS, status_code=status.HTTP_401_UNAUTHORIZED)


        # --- LOGIC THAT USES YOUR TABLES ---

        # 1. This line queries your `token_blacklist_outstandingtoken` table.
        #    It finds all active refresh tokens for the logged-in user.
        outstanding_tokens = OutstandingToken.objects.filter(user=user)
        # print(f"Outstanding tokens for user {userName}: {[token.token for token in outstanding_tokens]}")  # Debugging line

        # 2. This loop iterates through those tokens and adds a corresponding
        #    entry to your `token_blacklist_blacklistedtoken` table.
        #    This is what "invalidates" the old sessions.
        for token in outstanding_tokens:
            BlacklistedToken.objects.get_or_create(token=token)
            # print(f"Blacklisted token: {token.token}")  # Debugging line

        # --- END OF LOGIC ---
        user.token_invalidated_at = now()
        user.save(update_fields=["token_invalidated_at"])
        # The rest of your code remains the same.
        # It generates a NEW token for the new session.
        refresh = RefreshToken.for_user(user)
        refresh['auth_type'] = 'user_access'
        update_last_login(None, user)

        # Get reporting to name
        reporting_to_name = None
        if user.reportingToId:
            try:
                reporting_user = User.objects.get(id=user.reportingToId)
                reporting_to_name = reporting_user.fullName
            except User.DoesNotExist:
                reporting_to_name = None

        # Build response structure
        role_id = None
        role_name = None
        role_order_id = None
        group_number = None
        is_team_leader = False
        functionality_keys = []

        if hasattr(user, 'roleId') and user.roleId:
            role_id = user.roleId.roleId
            role_name = user.roleId.roleName
            role_order_id = user.roleId.roleOrderId
            group_number = user.roleId.groupNumber
            is_team_leader = user.roleId.isTeamLeader
            if user.roleId.functionalityKey:
                functionality_keys = user.roleId.functionalityKey

        employee_data = {
            "id": user.id,
            "userName": user.userName,
            "phoneNumber": user.phoneNumber,
            "employeeId": user.employeeId,
            "fullName": user.fullName,
            "nationality": user.nationality,
            "gender": user.gender,
            "dateOfBirth": user.dateOfBirth,
            "dateOfJoining": user.dateOfJoining,
            "shiftId": user.shiftId,
            "department": user.department,
            "reportingToId": user.reportingToId,
            "groupNumber": group_number,
            "employmentStatus": user.employmentStatus,
            "skillSet": user.skillSet,
            "reportingToName": reporting_to_name,
            "roleId": role_id,
            "roleName": role_name,
            "roleOrderId": role_order_id,
            "shiftUpdateTime": user.shiftUpdateTime.strftime("%Y-%m-%d %H:%M:%S") if user.shiftUpdateTime else None,  # Assuming you want to include this
            "functionalityKeys": functionality_keys,
            "isTeamLeader": is_team_leader
        }

        log_activity_raw(
            request=request,
            category='Authentication',
            action='Login',
            performer=user,  # The user is acting on their own behalf, not an admin performing an action
            target_employee_name=user.fullName,
            details={
                'userName': user.userName,
                'roleName': role_name  # It's useful to log the role for context
            }
        )


        response_data = {
            "employeeData": employee_data,
            "refreshToken": str(refresh),
            "jwt": str(refresh.access_token)
        }

        return success_response(data=response_data, message=LOGGED_IN_SUCCESSFULLY, status_code=status.HTTP_200_OK)

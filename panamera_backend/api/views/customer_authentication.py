from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from api.utils import execute_query
from types import SimpleNamespace
from rest_framework.permissions import BasePermission
from rest_framework.authentication import BaseAuthentication, TokenAuthentication



class CustomCustomerAuthentication(JWTAuthentication):
    """
    Custom authentication class for stateless customer JWTs.
    """
    def authenticate(self, request):
        user_auth_tuple = super().authenticate(request)
        if user_auth_tuple is None:
            return None

        customer, validated_token = user_auth_tuple

        token_iat = validated_token.get("iat")
        if not token_iat:
            raise AuthenticationFailed("Token missing 'iat' claim.")

        token_invalidated_timestamp = getattr(customer, "token_invalidated_at", None)
        if token_invalidated_timestamp and token_iat < int(token_invalidated_timestamp.timestamp()):
            raise AuthenticationFailed("This access token has been invalidated.")

        return customer, validated_token

    def get_user(self, validated_token):
        try:
            user_id = validated_token['user_id']
        except KeyError:
            raise AuthenticationFailed("Token contains no 'user_id' claim.", code="authentication_failed")

        query = 'SELECT * FROM customer WHERE id = %s AND "isDeleted" = 0 AND COALESCE("status", 0) = 0'
        result_list = execute_query(query, [user_id], fetch='one')

        if not result_list:
            raise AuthenticationFailed("Customer not found or account is inactive.", code="authentication_failed")

        customer_data = result_list[0]
        customer_obj = SimpleNamespace(**customer_data)
        customer_obj.is_authenticated = True
        customer_obj.pk = customer_data['id']
        customer_obj.is_customer = True  # Flag to identify as customer
        return customer_obj

class CombinedUserCustomerAuthentication(BaseAuthentication):
    """
    Combined authentication class that supports both user tokens (DRF TokenAuthentication)
    and customer JWTs (CustomCustomerAuthentication).
    """
    def authenticate(self, request):
        # Try user token authentication first (DRF TokenAuthentication)
        try:
            user_auth = TokenAuthentication().authenticate(request)
            if user_auth:
                user, token = user_auth
                setattr(user, 'is_customer', False)  # Flag to identify as user
                request.auth_type = 'user'
                return user_auth
        except AuthenticationFailed:
            pass  # Continue to try customer authentication

        # Try customer JWT authentication
        try:
            customer_auth = CustomCustomerAuthentication().authenticate(request)
            if customer_auth:
                customer, token = customer_auth
                request.auth_type = 'customer'
                return customer_auth
        except AuthenticationFailed:
            pass

        # If both fail, return None (authentication fails)
        return None

    def authenticate_header(self, request):
        # Return a generic header to satisfy DRF
        return 'Bearer'


class UserCustomerPermission(BasePermission):
    def has_permission(self, request, view):
        # Allow authenticated users or customers
        if not request.user or not getattr(request.user, 'is_authenticated', False):
            return False
        return True

    def has_object_permission(self, request, view, obj):
        # For PUT requests, restrict customers to their own tasks
        if request.method == 'PUT' and getattr(request.user, 'is_customer', False):
            # Fetch task to check customerId
            task_result = execute_query(
                'SELECT "customerId" FROM "taskManager" WHERE id = %s AND COALESCE("isDeleted", 0) = 0',
                [view.kwargs.get('task_id')], fetch='one'
            )
            task = task_result[0] if isinstance(task_result, list) and task_result else task_result
            if not task or task['customerId'] != request.user.customerId:
                return False
            return True
        return True

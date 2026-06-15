from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from api.models import User  # Your custom User model
from api.utils import execute_query  # Assuming execute_query is in here
from rest_framework_simplejwt.exceptions import InvalidToken, AuthenticationFailed
from types import SimpleNamespace


class AccessTokenInvalidationAuthentication(JWTAuthentication):
    """
    This class authenticates users via JWT and performs two key functions:
    1. It can authenticate both regular Users (employees) and Customers by inspecting
       a 'auth_type' claim in the JWT.
    2. It enforces token invalidation for Users based on the 'token_invalidated_at' field,
       ensuring that old tokens are rejected after a new login. This check is safely
       bypassed for Customers.
    """

    # [+] NEW METHOD
    # This method is the core of the new functionality. It's called internally
    # by the authentication process to fetch the user/customer object.
    def get_user(self, validated_token):
        """
        Overrides the default `get_user` to handle multiple user types ('user' vs 'customer')
        based on the 'auth_type' claim in the JWT.
        """
        # print("--- AUTHENTICATOR: get_user() CALLED ---")
        # print("VALIDATED TOKEN PAYLOAD:", validated_token)
        try:
            user_id = validated_token['user_id']
            auth_type = validated_token.get('auth_type') # Safely get the token type

            # Route 1: The token is for a regular User (employee)
            if auth_type == 'user_access':
                return User.objects.get(id=user_id)

            # Route 2: The token is for a Customer
            elif auth_type == 'customer_access':
                query = 'SELECT * FROM customer WHERE id = %s AND "isDeleted" = 0 AND status = 0'
                result_list = execute_query(query, [user_id], fetch='one')

                if not result_list:
                    # No active customer found for this ID
                    raise AuthenticationFailed("Customer not found or is inactive.", code='authentication_failed')

                # Create a simple object to represent the customer
                customer_data = result_list[0]
                customer = SimpleNamespace(**customer_data)

                # Add flags so our permission classes can identify this object
                customer.is_customer = True
                customer.is_authenticated = True
                return customer

            # Route 3: The token type is missing or unknown
            else:
                raise InvalidToken("Token is missing or has an invalid 'auth_type' claim.")

        except KeyError:
            # This happens if 'user_id' is not in the token
            raise InvalidToken("Token contained no recognizable user identification.")
        except User.DoesNotExist:
            # A user ID was in the token, but that user has been deleted
            raise AuthenticationFailed("User not found.", code='authentication_failed')


    # [*] MODIFIED METHOD
    # Your original method, made safer to handle different object types (User vs Customer).
    def authenticate(self, request):
        # This first line now calls our custom `get_user` method internally.
        # It will return `(User, token)` or `(Customer, token)`.
        user_auth_tuple = super().authenticate(request)
        if user_auth_tuple is None:
            return None

        user, validated_token = user_auth_tuple

        token_iat = validated_token.get("iat")
        if not token_iat:
            raise AuthenticationFailed("Token missing 'iat' claim.")
        
        # --- YOUR CRUCIAL LOGIC (NOW SAFER) ---

        # The following checks will only apply if the authenticated object is a `User`.
        # If it's a `Customer` (SimpleNamespace), these `getattr` calls will safely fail
        # and the invalidation logic will be correctly skipped.

        # 🚨 Skip invalidation check if roleId is 1 (for Users only)
        # We use `getattr` to prevent an error if `user` is a customer object.
        user_role_id = getattr(user, 'roleId_id', None)
        if user_role_id == 1:
            return user, validated_token

        # 🚨 Normal invalidation check for all other users
        token_invalidated_timestamp = getattr(user, 'token_invalidated_at', None)
        if token_invalidated_timestamp and token_iat < int(token_invalidated_timestamp.timestamp()):
            raise AuthenticationFailed("This access token has been invalidated due to a new login.")

        return user, validated_token

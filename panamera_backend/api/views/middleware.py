# middleware.py
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

class TokenValidationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip for login/refresh endpoints
        if request.method == 'OPTIONS':
            return self.get_response(request)

        if request.path in ['/api/login/', '/api/token/refresh/']:
            return self.get_response(request)

        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split()[1]
            if BlacklistedToken.objects.filter(token__token=token).exists():
                raise InvalidToken("Token has been invalidated by new login")

        return self.get_response(request)

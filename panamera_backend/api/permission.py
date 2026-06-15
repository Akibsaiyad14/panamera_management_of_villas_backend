from rest_framework.permissions import BasePermission
from api.models import User # Adjust the import path if needed


class IsAdminOrSupervisor(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.userRole.roleId in [1, 2]


from rest_framework.permissions import BasePermission
from api.models import User # Adjust the import path if needed

class IsUser(BasePermission):
    """ Allows access only to authenticated Users (employees). """
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and isinstance(request.user, User)

class IsCustomer(BasePermission):
    """ Allows access only to authenticated Customers. """
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and getattr(request.user, 'is_customer', False)

class IsUserOrCustomer(BasePermission):
    """ Allows access to any authenticated User or Customer. """
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

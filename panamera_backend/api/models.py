from django.db import models
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin
import base64


class UserManager(BaseUserManager):
    def create_user(self, userName, phoneNumber, userPassword, roleId, **extra_fields):
        if not userName:
            raise ValueError("The userName field must be set")
        if not phoneNumber:
            raise ValueError("The phoneNumber field must be set")
        if not userPassword:
            raise ValueError("The userPassword field must be set")

        # Encode password before storing
        encoded_password = base64.b64encode(userPassword.encode()).decode()

        user = self.model(userName=userName, phoneNumber=phoneNumber, userPassword=encoded_password, roleId=roleId, **extra_fields)
        user.save(using=self._db)
        return user

    def create_superuser(self, userName, phoneNumber, userPassword, roleId, **extra_fields):
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_staff", True)
        return self.create_user(userName, phoneNumber, userPassword, roleId, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.AutoField(primary_key=True, db_column="id")
    roleId = models.ForeignKey("Userrole", on_delete=models.CASCADE, db_column="roleId")
    userName = models.CharField(db_column="userName", unique=True, max_length=40)
    fullName = models.CharField(db_column="fullName", max_length=255, null=True, blank=True)
    phoneNumber = models.CharField(db_column="phoneNumber", unique=True, max_length=255)
    password = models.CharField(db_column="userPassword", max_length=100)
    reportingToId = models.IntegerField(db_column="reportingToId", null=True, blank=True)  # Supervisor hierarchy - DO NOT MODIFY
    teamLeaderId = models.IntegerField(db_column="teamLeaderId", null=True, blank=True)  # Team assignment - separate from supervisor
    createdDate = models.DateTimeField(db_column="createdDate", auto_now_add=True)

    isDeleted = models.CharField(
        db_column="isDeleted",
        max_length=1,  # Length should be sufficient for '0' or '1'
        default='0',   # Default value as a string
        choices=[('0', 'Not Deleted'), ('1', 'Deleted')] # Optional: helpful for forms/admin
    )
    last_login = models.DateTimeField(db_column="last_login", null=True, blank=True)
    is_superuser = models.BooleanField(db_column="is_superuser", default=False)
    is_staff = models.BooleanField(db_column="is_staff", default=False)
    name = models.CharField(db_column="name", max_length=255, null=True, blank=True)
    empCode = models.CharField(db_column="empCode", max_length=255, null=True, blank=True)
    employeeId = models.CharField(db_column="employeeId", max_length=50, null=True, blank=True)

    # --- Other fields from your schema that might be missing ---
    nationality = models.CharField(max_length=100, db_column="nationality", null=True, blank=True)
    gender = models.CharField(max_length=10, db_column="gender", default='Male')
    dateOfBirth = models.DateField(db_column="dateOfBirth", null=True, blank=True)
    dateOfJoining = models.DateField(db_column="dateOfJoining", null=True, blank=True)
    department = models.CharField(max_length=150, db_column="department", null=True, blank=True)
    employmentStatus = models.CharField(max_length=50, db_column="employmentStatus", default='Active')
    skillSet = models.TextField(db_column="skillSet", null=True, blank=True)
    shiftId = models.IntegerField(db_column="shiftId", null=True, blank=True)
    shiftUpdateTime = models.DateTimeField(db_column="shiftUpdateTime", null=True, blank=True)
    token_invalidated_at = models.DateTimeField(null=True, blank=True)
    # trnNo = models.CharField(db_column="trnNo", max_length=255, null=True, blank=True)
    USERNAME_FIELD = "userName"  # Authenticate using userName
    REQUIRED_FIELDS = ["phoneNumber", "roleId"]

    objects = UserManager()

    class Meta:
        managed = False  # Don't let Django auto-create tables
        db_table = "user"

    # def check_password(self, raw_password):
    #     """Decode Base64 password and check if it matches."""
    #     decoded_password = base64.b64decode(self.userPassword.encode()).decode()
    #     return decoded_password == raw_password

    def check_password(self, raw_password):
        decoded_password = base64.b64decode(self.password.encode()).decode()
        return decoded_password == raw_password

    def __str__(self):
        return self.userName


class Userrole(models.Model):
    roleId = models.AutoField(db_column="roleId", primary_key=True)
    roleName = models.CharField(db_column="roleName", unique=True, max_length=255)
    reportingToRoleId = models.IntegerField(db_column="reportingToRoleId", null=True, blank=True)
    roleOrderId = models.IntegerField(db_column="roleOrderId", null=True, blank=True)
    functionalityKey = models.JSONField(db_column="functionalityKey", null=True, blank=True)  # Assuming this is a JSON field
    groupNumber = models.IntegerField(db_column="groupNumber", null=True, blank=True)
    isTeamLeader = models.BooleanField(db_column="isTeamLeader", default=False)  # New field to indicate if the role is a team leader


    class Meta:
        managed = False
        db_table = "userrole"

    def __str__(self):
        return self.roleName

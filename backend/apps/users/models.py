import uuid

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models

from common.models import Entity, TenantEntity


class Tenant(Entity):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(default=True)
    acl_epoch = models.PositiveBigIntegerField(default=1)
    quota_config = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, is_tenant_admin=True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    display_name = models.CharField(max_length=100, blank=True)
    is_tenant_admin = models.BooleanField(default=False)
    roles = models.ManyToManyField("Role", through="UserRole", related_name="users")
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["tenant_id"]
    objects = UserManager()

    def __str__(self):
        return self.email


class Permission(Entity):
    code = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=200, blank=True)


class Role(TenantEntity):
    code = models.CharField(max_length=80)
    name = models.CharField(max_length=100)
    permissions = models.ManyToManyField(Permission, blank=True)

    class Meta(TenantEntity.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "code"], name="role_tenant_code")]


class UserRole(TenantEntity):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta(TenantEntity.Meta):
        constraints = [models.UniqueConstraint(fields=["user", "role"], name="unique_user_role")]

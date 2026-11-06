from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import F

from .models import Permission, Role, Tenant, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "tenant", "is_tenant_admin", "is_active"]
    search_fields = ["email", "display_name"]
    fieldsets = [
        (None, {"fields": ["email", "password", "tenant", "display_name"]}),
        ("Permissions", {"fields": ["is_active", "is_tenant_admin", "is_staff", "is_superuser"]}),
    ]
    add_fieldsets = [(None, {"classes": ["wide"], "fields": ["email", "tenant", "password1", "password2"]})]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and {"is_active", "is_tenant_admin", "is_superuser"} & set(form.changed_data):
            Tenant.objects.filter(pk=obj.tenant_id).update(acl_epoch=F("acl_epoch") + 1)


class OperatorReadOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in [Tenant, Role, Permission]:
    admin.site.register(model, OperatorReadOnlyAdmin)

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import AuditEvent, Invitation, Organization, User


class PlatformOnly:
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(User)
class PlatformUserAdmin(PlatformOnly, UserAdmin):
    ordering = ["email"]
    list_display = ["email", "is_active", "is_superuser"]
    search_fields = ["email"]
    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        ("Profile", {"fields": ["first_name", "last_name", "avatar_url"]}),
        ("Platform access", {"fields": ["is_active", "is_developer", "is_staff", "is_superuser"]}),
    ]
    add_fieldsets = [(None, {"fields": ["email", "password1", "password2"]})]


@admin.register(Organization)
class OrganizationAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ["name", "slug", "active"]
    exclude = ["permission_tags"]  # Tenant tag changes use the policy-enforced portal.


@admin.register(AuditEvent)
class AuditAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ["created", "organization", "actor", "action", "target"]
    readonly_fields = ["organization", "actor", "actor_subject", "principal_id", "principal_kind", "request_id", "action", "target", "created", "outcome", "reason_code", "purpose_code", "tags"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Toolkit model administration permits changing protocol security settings and
# would bypass tenant policy. Application registrations belong in the tenant portal.
from oauth2_provider.models import get_access_token_model, get_application_model, get_grant_model, get_id_token_model, get_refresh_token_model
for model in [get_access_token_model(), get_application_model(), get_grant_model(), get_id_token_model(), get_refresh_token_model()]:
    if admin.site.is_registered(model):
        admin.site.unregister(model)

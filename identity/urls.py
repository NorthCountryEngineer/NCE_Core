from django.urls import path
from . import views
from . import security_portal
from . import app_accounts

urlpatterns = [
    path("api/v1/application-accounts/", app_accounts.application_accounts, name="application_accounts"),
    path("accounts/profile/", views.profile, name="profile"),
    path("organizations/<slug:slug>/access-purpose/", security_portal.access_purpose, name="access_purpose"),
    path("organizations/<slug:slug>/services/<uuid:service_pk>/resources/new/", security_portal.protected_resource_edit, name="protected_resource_add"),
    path("organizations/<slug:slug>/services/<uuid:service_pk>/resources/<uuid:pk>/", security_portal.protected_resource_edit, name="protected_resource"),
    path("organizations/<slug:slug>/security/", security_portal.security, name="security"),
    path("organizations/<slug:slug>/security/policy/", security_portal.policy_edit, name="authentication_policy"),
    path("organizations/<slug:slug>/security/providers/new/", security_portal.provider_edit, name="provider_add"),
    path("organizations/<slug:slug>/security/providers/<uuid:pk>/", security_portal.provider_edit, name="provider_edit"),
    path("organizations/<slug:slug>/security/identities/new/", security_portal.identity_link, name="identity_link_add"),
    path("organizations/<slug:slug>/security/identities/<int:pk>/", security_portal.identity_link, name="identity_link"),
    path("organizations/<slug:slug>/security/tags/new/", security_portal.tag_edit, name="permission_tag_add"),
    path("organizations/<slug:slug>/security/tags/<int:pk>/", security_portal.tag_edit, name="permission_tag"),
    path("organizations/<slug:slug>/security/resources/<str:resource_type>/<str:resource_id>/", security_portal.resource_tags, name="resource_tags"),
    path("", views.home, name="home"),
    path("health/", views.health, name="health"),
    path("invitations/<str:token>/", views.accept_invitation, name="accept_invitation"),
    path("organizations/<slug:slug>/", views.organization_detail, name="organization"),
    path("organizations/<slug:slug>/settings/", views.branding, name="branding"),
    path("organizations/<slug:slug>/invite/", views.invite, name="invite"),
    path("organizations/<slug:slug>/invitations/<int:pk>/revoke/", views.revoke_invitation, name="revoke_invitation"),
    path("organizations/<slug:slug>/members/<int:pk>/", views.membership_edit, name="membership"),
    path("organizations/<slug:slug>/roles/new/", views.role_edit, name="role_add"),
    path("organizations/<slug:slug>/roles/<int:pk>/", views.role_edit, name="role"),
    path("organizations/<slug:slug>/services/new/", views.service_edit, name="service_add"),
    path("organizations/<slug:slug>/services/<uuid:pk>/", views.service_detail, name="service"),
    path("organizations/<slug:slug>/services/<uuid:pk>/settings/", views.service_edit, name="service_edit"),
    path("organizations/<slug:slug>/services/<uuid:pk>/permissions/new/", views.permission_add, name="permission_add"),
    path("organizations/<slug:slug>/services/<uuid:service_pk>/clients/new/", views.application_edit, name="application_add"),
    path("organizations/<slug:slug>/services/<uuid:service_pk>/clients/<int:pk>/", views.application_edit, name="application"),
    path("organizations/<slug:slug>/services/<uuid:service_pk>/clients/<int:pk>/rotate/", views.rotate_client_secret, name="rotate_client_secret"),
    path("api/v1/authorize/", views.authorize_permission, name="authorize_permission"),
]

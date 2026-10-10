from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from oauth2_provider import views as oauth_views
from identity import views
from identity import federation

oauth_patterns = [
    path("authorize/", views.TenantAuthorizationView.as_view(), name="authorize"),
    path("token/", oauth_views.TokenView.as_view(), name="token"),
    path("revoke/", oauth_views.RevokeTokenView.as_view(), name="revoke-token"),
    path("introspect/", views.introspect, name="introspect"),
    path("userinfo/", oauth_views.UserInfoView.as_view(), name="user-info"),
    path("jwks/", oauth_views.JwksInfoView.as_view(), name="jwks-info"),
]
urlpatterns = [
    path("platform-admin/", admin.site.urls),
    path("accounts/login/", federation.ProviderLoginView.as_view(), name="login"),
    path("accounts/providers/<uuid:pk>/begin/", federation.begin, name="provider_begin"),
    path("accounts/providers/<uuid:pk>/callback/", federation.callback, name="provider_callback"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password/", auth_views.PasswordChangeView.as_view(template_name="identity/form.html",
         extra_context={"title": "Change password"}, success_url="/"), name="password_change"),
    path(".well-known/openid-configuration", views.DiscoveryView.as_view(), name="discovery"),
    path(".well-known/oauth-authorization-server", oauth_views.OAuthServerMetadataView.as_view()),
    path("oauth/", include((oauth_patterns, "oauth2_provider"), namespace="oauth2_provider")),
    path("", include("platform_services.urls")),
    path("", include("core_chat.urls")),
    path("", include("core_fabric.urls")),
    path("", include("identity.urls")),
]

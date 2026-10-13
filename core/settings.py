import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("CORE_DEBUG") == "1"
PRODUCT_NAME = os.environ.get("CORE_PRODUCT_NAME", "Core Platform")


def read_secret(variable, development_filename):
    filename = os.environ.get(variable)
    if not filename and DEBUG:
        filename = str(BASE_DIR / ".local" / development_filename)
    if not filename:
        raise ImproperlyConfigured(f"Set {variable} to a mounted secret file for production. "
            "For laptop development, use python manage.py --local migrate or python manage.py dev.")
    try:
        value = Path(filename).read_text().strip()
    except (OSError, UnicodeError) as error:
        raise ImproperlyConfigured(f"{variable} must point to a readable secret file. "
            "For laptop development, use python manage.py dev to initialize missing local keys.") from error
    if not value:
        raise ImproperlyConfigured(f"{variable} points to an empty secret file; supply a valid key.")
    return value


SECRET_KEY = read_secret("CORE_SECRET_KEY_FILE", "django.key")
OIDC_PRIVATE_KEY = read_secret("CORE_OIDC_KEY_FILE", "oidc.key")
ISSUER = os.environ.get("CORE_ISSUER", "http://localhost:8000" if DEBUG else "").rstrip("/")
# A platform verification is read-only. Resource creation additionally requires
# this operator switch and an explicit per-release approval of AWS charges.
CORE_ENABLE_CLOUD_DEPLOYMENTS = os.environ.get('CORE_ENABLE_CLOUD_DEPLOYMENTS') == '1'
issuer_url = urlparse(ISSUER)
if not issuer_url.netloc or issuer_url.path or issuer_url.query or issuer_url.fragment or issuer_url.username:
    raise ImproperlyConfigured("CORE_ISSUER must be an absolute origin URL, without a path or credentials.")
if issuer_url.scheme != "https" and not (DEBUG and issuer_url.scheme == "http"):
    raise ImproperlyConfigured("CORE_ISSUER must use HTTPS outside local development.")
ALLOWED_HOSTS = os.environ.get("CORE_ALLOWED_HOSTS", issuer_url.hostname).split(",")
CSRF_TRUSTED_ORIGINS = [ISSUER]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "oauth2_provider", "identity", "platform_services", "core_chat", "core_fabric", "core_ai",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "identity.browser_cors.OAuthBrowserCorsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "identity.access.SecurityContextMiddleware",
    "platform_services.middleware.DeveloperOnboardingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "core.urls"
WSGI_APPLICATION = "core.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "identity.branding.context",
    ]},
}]
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    parsed = urlparse(DATABASE_URL)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use PostgreSQL.")
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""), "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname, "PORT": parsed.port or 5432,
        "OPTIONS": {k: v[0] for k, v in parse_qs(parsed.query).items()},
    }}
elif DEBUG:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / ".local" / "db.sqlite3"}}
else:
    raise ImproperlyConfigured("Set DATABASE_URL for production (PostgreSQL).")
AUTH_USER_MODEL = "identity.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
# Keep same-origin form POSTs identifiable to CSRF checks. `no-referrer`
# makes browsers send Origin: null; same-origin still hides external referrers.
SECURE_REFERRER_POLICY = "same-origin"
# Enable only behind a proxy that strips and sets this header itself.
if os.environ.get("CORE_TRUST_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
OAUTH2_PROVIDER_APPLICATION_MODEL = "identity.Application"
OAUTH2_PROVIDER = {
    "OAUTH2_BACKEND_CLASS": "identity.oauth_backend.EvidenceOAuthLibCore",
    "OAUTH2_VALIDATOR_CLASS": "identity.oauth.OrganizationValidator",
    "OIDC_ENABLED": True,
    "OIDC_ISS_ENDPOINT": ISSUER,
    "OIDC_RSA_PRIVATE_KEY": OIDC_PRIVATE_KEY,
    "OIDC_RSA_PRIVATE_KEYS_INACTIVE": [
        Path(p).read_text() for p in os.environ.get("CORE_OIDC_PREVIOUS_KEY_FILES", "").split(",") if p
    ],
    "SCOPES": {"openid": "Sign in", "profile": "Basic profile", "email": "Email address",
               "authorization": "Organization roles and service permissions"},
    "DEFAULT_SCOPES": ["openid"],
    "ACCESS_TOKEN_EXPIRE_SECONDS": 300,
    "ID_TOKEN_EXPIRE_SECONDS": 300,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 86400,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "PKCE_REQUIRED": True,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["http", "https"] if DEBUG else ["https"],
    "ALLOW_URI_WILDCARDS": False,
    "OIDC_RESPONSE_TYPES_SUPPORTED": ["code"],
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
    **{f"COMPLIANT_BCP_RFC9700_{name}": True for name in [
        "IMPLICIT_GRANT", "PASSWORD_GRANT", "PKCE_METHOD", "ACCESS_TOKEN_TRANSPORT",
        "AUTHZ_RESPONSE_ISS", "TOKEN_STORAGE", "REFRESH_TOKEN", "REDIRECT_URI_MATCHING", "PKCE_REQUIRED",
    ]},
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": not DEBUG,
}

# Tenant administrators cannot make the broker fetch arbitrary internal URLs or files.
CORE_IDP_ALLOWED_HOSTS = [value.strip().lower() for value in os.environ.get("CORE_IDP_ALLOWED_HOSTS", "").split(",") if value.strip()]
CORE_IDP_SECRET_ROOT = os.environ.get("CORE_IDP_SECRET_ROOT", str(BASE_DIR / ".local" / "idp-secrets") if DEBUG else "/run/secrets/identity-providers")
CORE_CHAT_SECRET_ROOT = os.environ.get('CORE_CHAT_SECRET_ROOT', str(BASE_DIR / '.local' / 'chat-federation') if DEBUG else '/run/secrets/chat-federation')
CORE_CHAT_FEDERATION_ORIGINS = [value.strip() for value in os.environ.get('CORE_CHAT_FEDERATION_ORIGINS', '').split(',') if value.strip()]
CORE_FABRIC_SECRET_ROOT = os.environ.get('CORE_FABRIC_SECRET_ROOT', str(BASE_DIR / '.local' / 'data-fabric') if DEBUG else '/run/secrets/data-fabric')
CORE_FABRIC_ORIGINS = [value.strip() for value in os.environ.get('CORE_FABRIC_ORIGINS', '').split(',') if value.strip()]

# Fargate receives secrets from ECS and the entrypoint writes private files.
# Keep this opt-in so existing local/Kubernetes deployments keep their boundary.
if os.environ.get("CORE_AWS_ORIGIN_BOUNDARY") == "1":
    CORE_ORIGIN_SECRET = read_secret("CORE_ORIGIN_SECRET_FILE", "origin.key")
    if len(CORE_ORIGIN_SECRET) < 32:
        raise ImproperlyConfigured("CORE_ORIGIN_SECRET_FILE must contain at least 32 characters.")
    if os.environ.get("CORE_TRUST_PROXY") != "1":
        raise ImproperlyConfigured("The AWS origin boundary requires CORE_TRUST_PROXY=1.")
    MIDDLEWARE.insert(0, "core.aws.AWSOriginMiddleware")

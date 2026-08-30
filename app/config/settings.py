import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent


def env_bool(name, default=False):
    return os.getenv(name, "1" if default else "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)
USE_SQLITE = env_bool("VOBIA_USE_SQLITE", False)

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or USE_SQLITE:
        SECRET_KEY = "local-development-only-not-for-production"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY wajib diisi di luar local test.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if RENDER_EXTERNAL_HOSTNAME:
    if RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    render_origin = f"https://{RENDER_EXTERNAL_HOSTNAME}"
    if render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(render_origin)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "audit",
    "master_data",
    "imports",
    "sales",
    "merchandising",
    "purchasing",
    "production",
    "inventory",
    "reconciliation",
    "traffic",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

if USE_SQLITE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": PROJECT_ROOT / "data" / "vobia_erp.sqlite3",
        }
    }
elif os.getenv("DATABASE_URL", "").strip():
    from urllib.parse import unquote, urlparse

    database_url = urlparse(os.environ["DATABASE_URL"].strip())
    if database_url.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL harus PostgreSQL.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database_url.path.lstrip("/"),
            "USER": unquote(database_url.username or ""),
            "PASSWORD": unquote(database_url.password or ""),
            "HOST": database_url.hostname or "",
            "PORT": str(database_url.port or 5432),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {
                "connect_timeout": 5,
                "sslmode": os.getenv("POSTGRES_SSLMODE", "require"),
            },
        }
    }
else:
    required_database_settings = {
        "NAME": os.getenv("POSTGRES_DB", ""),
        "USER": os.getenv("POSTGRES_USER", ""),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", ""),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
    missing = [key for key, value in required_database_settings.items() if not value]
    if missing:
        raise ImproperlyConfigured(
            "Konfigurasi PostgreSQL belum lengkap: " + ", ".join(missing)
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            **required_database_settings,
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"connect_timeout": 5},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "id"
TIME_ZONE = "Asia/Jakarta"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = PROJECT_ROOT / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or USE_SQLITE
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
PRIVATE_UPLOAD_ROOT = Path(
    os.getenv("VOBIA_PRIVATE_UPLOAD_ROOT", PROJECT_ROOT / "data" / "private_uploads")
)
INSTAGRAM_CONNECTION_DIR = Path(
    os.getenv("INSTAGRAM_CONNECTION_DIR", PROJECT_ROOT / "data" / "private_integrations")
)
INSTAGRAM_LIVE_ENABLED = env_bool("INSTAGRAM_LIVE_ENABLED", False)
MEDIA_ROOT = PRIVATE_UPLOAD_ROOT
MASTER_IMPORT_MAX_BYTES = 25 * 1024 * 1024
MASTER_IMPORT_MAX_ROWS = 5000
SALES_IMPORT_MAX_ROWS = 100000
SALES_IMPORT_COMMIT_ENABLED = env_bool("SALES_IMPORT_COMMIT_ENABLED", True)
ALLOW_INITIAL_SETUP_PAGE = env_bool("VOBIA_ALLOW_INITIAL_SETUP", DEBUG or USE_SQLITE)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "accounts:login"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = 0 if DEBUG else int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)

LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCKOUT_MINUTES = 15

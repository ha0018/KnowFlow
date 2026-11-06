import hashlib
from datetime import timedelta
from pathlib import Path

import environ
from botocore.config import Config

BASE_DIR = Path(__file__).resolve().parents[2]
env = environ.Env()
environ.Env.read_env(BASE_DIR.parent / ".env")
DEBUG = env.bool("DEBUG", default=False)
SECRET_KEY = env("SECRET_KEY", default="local-development-only-change-before-deployment")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "testserver"])
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "apps.users",
    "apps.knowledge",
    "apps.documents",
    "apps.conversations",
    "apps.agents",
    "apps.evaluation",
    "apps.monitoring",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "common.middleware.TraceMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DATABASES["default"]["CONN_MAX_AGE"] = 0  # ASGI: use independent short-lived DB connections.
AUTH_USER_MODEL = "users.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = env.path("MEDIA_ROOT", default=str(BASE_DIR / "media"))
# Private documents are served through authenticated views, never via MEDIA_URL.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
S3_ENABLED = env.bool("S3_ENABLED", default=False)
if S3_ENABLED:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "access_key": env("S3_ACCESS_KEY"),
            "secret_key": env("S3_SECRET_KEY"),
            "bucket_name": env("S3_BUCKET", default="knowflow"),
            "endpoint_url": env("S3_ENDPOINT"),
            "region_name": env("S3_REGION", default="us-east-1"),
            "default_acl": None,
            "querystring_auth": True,
            "file_overwrite": False,
            "client_config": Config(
                signature_version="s3v4",
                connect_timeout=2,
                read_timeout=3,
                retries={"max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        },
    }
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["common.auth.TenantJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 30,
    "EXCEPTION_HANDLER": "common.exceptions.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "120/min", "anon": "10/min", "login": "10/min"},
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "idempotency-key",
    "x-csrftoken",
    "x-trace-id",
]
CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_IGNORE_RESULT = True
# The database outbox owns publication retries; HTTP uploads should fail fast
# when Redis is down, while workers keep reconnecting independently.
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = None
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "socket_connect_timeout": 2,
    "socket_timeout": 2,
    "max_retries": 0,
    "visibility_timeout": 3600,
}
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_ROUTES = {
    "apps.documents.tasks.*": {"queue": "documents"},
    "apps.evaluation.tasks.*": {"queue": "evaluation"},
}
CELERY_BEAT_SCHEDULE = {"recover-work": {"task": "apps.documents.tasks.recover_work", "schedule": 30.0}}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_PAGES = 200
MAX_PARSED_CHARS = 2_000_000
AI_PROVIDER = env("AI_PROVIDER", default="demo")
AI_BASE_URL = env("AI_BASE_URL", default="https://api.openai.com/v1")
AI_API_KEY = env("AI_API_KEY", default="")
AI_MODEL = env("AI_MODEL", default="")
EMBEDDING_PROVIDER = env("EMBEDDING_PROVIDER", default="demo")
EMBEDDING_MODEL = env("EMBEDDING_MODEL", default="demo-hash-v1")
EMBEDDING_DIMENSIONS = env.int("EMBEDDING_DIMENSIONS", default=128)
EMBEDDING_PROFILE = f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}:{EMBEDDING_DIMENSIONS}:tokens-v1"
AI_TIMEOUT_SECONDS = env.int("AI_TIMEOUT_SECONDS", default=60)
MAX_CONCURRENT_RUNS = env.int("MAX_CONCURRENT_RUNS", default=3)
# Empty overrides retain the original AI_* settings for existing installations.
GENERATION_PROVIDER = env("GENERATION_PROVIDER", default="")
GENERATION_BASE_URL = env("GENERATION_BASE_URL", default="")
GENERATION_API_KEY = env("GENERATION_API_KEY", default="")
GENERATION_MODEL = env("GENERATION_MODEL", default="")
GENERATION_AUTH_REQUIRED = env.bool("GENERATION_AUTH_REQUIRED", default=True)
GENERATION_TOKEN_PARAMETER = env("GENERATION_TOKEN_PARAMETER", default="max_completion_tokens")
GENERATION_CONTEXT_WINDOW = env.int("GENERATION_CONTEXT_WINDOW", default=16384)
GENERATION_INPUT_BUDGET = env.int("GENERATION_INPUT_BUDGET", default=12000)
GENERATION_HISTORY_BUDGET = env.int("GENERATION_HISTORY_BUDGET", default=2000)
GENERATION_MAX_OUTPUT_TOKENS = env.int("GENERATION_MAX_OUTPUT_TOKENS", default=800)
GENERATION_TIMEOUT_SECONDS = env.int("GENERATION_TIMEOUT_SECONDS", default=AI_TIMEOUT_SECONDS)
GENERATION_INPUT_USD_PER_MILLION = env("GENERATION_INPUT_USD_PER_MILLION", default="")
GENERATION_OUTPUT_USD_PER_MILLION = env("GENERATION_OUTPUT_USD_PER_MILLION", default="")
EMBEDDING_BASE_URL = env("EMBEDDING_BASE_URL", default="")
EMBEDDING_API_KEY = env("EMBEDDING_API_KEY", default="")
EMBEDDING_AUTH_REQUIRED = env.bool("EMBEDDING_AUTH_REQUIRED", default=True)
EMBEDDING_SEND_DIMENSIONS = env.bool("EMBEDDING_SEND_DIMENSIONS", default=False)
EMBEDDING_PROFILE_VERSION = env("EMBEDDING_PROFILE_VERSION", default="v1")
EMBEDDING_TIMEOUT_SECONDS = env.int("EMBEDDING_TIMEOUT_SECONDS", default=45)
EMBEDDING_MAX_INPUT_TOKENS = env.int("EMBEDDING_MAX_INPUT_TOKENS", default=8192)
EMBEDDING_USD_PER_MILLION = env("EMBEDDING_USD_PER_MILLION", default="")
MODEL_PRICE_VERSION = env("MODEL_PRICE_VERSION", default="unpriced")
MODEL_MAX_RETRIES = env.int("MODEL_MAX_RETRIES", default=2)
MODEL_RETRY_MAX_DELAY_SECONDS = env.float("MODEL_RETRY_MAX_DELAY_SECONDS", default=2)
DEFAULT_TENANT_DAILY_TOKENS = env.int("DEFAULT_TENANT_DAILY_TOKENS", default=1_000_000)
DEFAULT_USER_DAILY_TOKENS = env.int("DEFAULT_USER_DAILY_TOKENS", default=100_000)
if EMBEDDING_PROVIDER != "demo":
    endpoint_fingerprint = hashlib.sha256(
        f"{(EMBEDDING_BASE_URL or AI_BASE_URL).rstrip('/')}|dimensions={EMBEDDING_SEND_DIMENSIONS}".encode()
    ).hexdigest()[:16]
    EMBEDDING_PROFILE = f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}:{EMBEDDING_DIMENSIONS}:{EMBEDDING_PROFILE_VERSION}:{endpoint_fingerprint}:tokens-v1"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "common.middleware.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

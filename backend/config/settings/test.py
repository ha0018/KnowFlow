from .base import *  # noqa: F403

SECRET_KEY = "test-key-with-sufficient-length-for-jwt-tests"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
AI_PROVIDER = "demo"
EMBEDDING_PROVIDER = "demo"
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}  # noqa: F405

import os
import tempfile
from pathlib import Path

from .base import *  # noqa: F403

# The real killable tool worker must share committed fixtures with its parent.
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):  # noqa: F405
    DATABASES["default"]["TEST"] = {  # noqa: F405
        "NAME": str(Path(tempfile.gettempdir()) / f"knowflow-test-{os.getpid()}.sqlite3")
    }

SECRET_KEY = "test-key-with-sufficient-length-for-jwt-tests"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
AI_PROVIDER = "demo"
EMBEDDING_PROVIDER = "demo"
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}  # noqa: F405

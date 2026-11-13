import os
import tempfile
from pathlib import Path

from .base import *  # noqa: F403
from .base import DATABASES

# The real killable tool worker must share committed fixtures with its parent.
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    DATABASES["default"]["TEST"] = {
        "NAME": str(Path(tempfile.gettempdir()) / f"knowflow-test-{os.getpid()}.sqlite3")
    }
    DATABASES["default"].setdefault("OPTIONS", {}).update(
        {"init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"}
    )

SECRET_KEY = "test-key-with-sufficient-length-for-jwt-tests"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
AI_PROVIDER = "demo"
EMBEDDING_PROVIDER = "demo"
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}  # noqa: F405

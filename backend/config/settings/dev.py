from .base import *  # noqa: F403

DEBUG = True
# No Redis required for the local demonstration. Production uses the worker.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)  # noqa: F405

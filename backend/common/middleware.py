import json
import logging
import re
import time
import uuid

from asgiref.sync import iscoroutinefunction
from django.utils.decorators import sync_and_async_middleware


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {"level": record.levelname, "logger": record.name, "message": record.getMessage()},
            ensure_ascii=False,
        )


@sync_and_async_middleware
def TraceMiddleware(get_response):
    def before(request):
        supplied = request.headers.get("X-Trace-ID", "")
        request.trace_id = supplied if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", supplied) else uuid.uuid4().hex
        return time.monotonic()

    def after(request, response, start):
        response["X-Trace-ID"] = request.trace_id
        logging.getLogger("knowflow.http").info(
            json.dumps(
                {
                    "trace_id": request.trace_id,
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                },
                ensure_ascii=False,
            )
        )
        return response

    if iscoroutinefunction(get_response):

        async def middleware(request):
            start = before(request)
            return after(request, await get_response(request), start)
    else:

        def middleware(request):
            start = before(request)
            return after(request, get_response(request), start)

    return middleware

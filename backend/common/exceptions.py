from rest_framework.views import exception_handler as drf_handler


def exception_handler(exc, context):
    response = drf_handler(exc, context)
    if response is not None:
        response.data = {"error": response.data, "trace_id": getattr(context.get("request"), "trace_id", "")}
    return response

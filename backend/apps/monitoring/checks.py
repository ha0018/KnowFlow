from django.core.checks import Error, register

from ai_engine.gateway.config import GatewayError, profile


@register()
def model_configuration(app_configs, **kwargs):
    errors = []
    from django.conf import settings

    if not 1 <= settings.CLEANUP_STORAGE_GRACE_DAYS <= 365:
        errors.append(Error("Invalid cleanup retention", id="knowflow.E004"))
    from rest_framework.throttling import UserRateThrottle

    try:
        UserRateThrottle().parse_rate(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["user"])
    except (ValueError, KeyError, TypeError):
        errors.append(Error("Invalid API_USER_RATE", id="knowflow.E005"))
    from ai_engine.parsers import ParseError
    from ai_engine.parsers.isolation import parser_limits

    try:
        parser_limits()
    except ParseError as exc:
        errors.append(Error(str(exc), id="knowflow.E003"))
    from apps.agents.policy import limits

    try:
        limits()
    except GatewayError as exc:
        errors.append(Error(exc.code, id="knowflow.E002"))
    for kind in ["generation", "embedding"]:
        try:
            profile(kind)
        except GatewayError as exc:
            errors.append(Error(f"{kind}: {exc.code}", id="knowflow.E001"))
    return errors

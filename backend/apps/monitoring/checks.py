from django.core.checks import Error, register

from ai_engine.gateway.config import GatewayError, profile


@register()
def model_configuration(app_configs, **kwargs):
    errors = []
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

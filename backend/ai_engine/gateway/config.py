from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings


class GatewayError(ValueError):
    """Safe public code; upstream bodies, request headers and secrets are excluded."""

    def __init__(self, code, *, retryable=False, retry_after=None, status=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


@dataclass(frozen=True)
class ModelProfile:
    kind: str
    provider: str
    base_url: str
    api_key: str
    model: str
    auth_required: bool
    timeout: int
    input_price: str
    output_price: str = ""

    def public(self):
        # Endpoint URLs can contain private host names; credentials and paths
        # never enter the public status, run snapshots or model-call logs.
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "timeout_seconds": self.timeout,
            "auth_required": self.auth_required,
            "key_configured": bool(self.api_key),
        }

    def headers(self):
        return {"Authorization": "Bearer " + self.api_key} if self.api_key else {}

    def prices(self):
        return {
            "currency": "USD",
            "input_per_million": self.input_price or None,
            "output_per_million": self.output_price or None,
            "version": settings.MODEL_PRICE_VERSION,
        }


def profile(kind):
    if kind == "generation":
        result = ModelProfile(
            kind,
            settings.GENERATION_PROVIDER or settings.AI_PROVIDER,
            settings.GENERATION_BASE_URL or settings.AI_BASE_URL,
            settings.GENERATION_API_KEY or (settings.AI_API_KEY if not settings.GENERATION_BASE_URL else ""),
            settings.GENERATION_MODEL or settings.AI_MODEL,
            settings.GENERATION_AUTH_REQUIRED,
            settings.GENERATION_TIMEOUT_SECONDS,
            settings.GENERATION_INPUT_USD_PER_MILLION,
            settings.GENERATION_OUTPUT_USD_PER_MILLION,
        )
    elif kind == "embedding":
        result = ModelProfile(
            kind,
            settings.EMBEDDING_PROVIDER,
            settings.EMBEDDING_BASE_URL or settings.AI_BASE_URL,
            settings.EMBEDDING_API_KEY or (settings.AI_API_KEY if not settings.EMBEDDING_BASE_URL else ""),
            settings.EMBEDDING_MODEL,
            settings.EMBEDDING_AUTH_REQUIRED,
            settings.EMBEDDING_TIMEOUT_SECONDS,
            settings.EMBEDDING_USD_PER_MILLION,
        )
    else:
        raise GatewayError("MODEL_KIND_INVALID")
    if result.provider not in {"demo", "compatible"}:
        raise GatewayError("MODEL_PROVIDER_INVALID")
    if not 1 <= result.timeout <= 120:
        raise GatewayError("MODEL_TIMEOUT_CONFIG_INVALID")
    if result.provider != "demo":
        try:
            url = urlsplit(result.base_url)
            url.port
        except ValueError:
            raise GatewayError("MODEL_ENDPOINT_INVALID") from None
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(c.isspace() for c in result.base_url)
        ):
            raise GatewayError("MODEL_ENDPOINT_INVALID")
        if not result.model or len(result.model) > 120 or (result.auth_required and not result.api_key):
            raise GatewayError("MODEL_NOT_CONFIGURED")
        if any(ord(c) < 32 for c in result.api_key):
            raise GatewayError("MODEL_KEY_INVALID")
    for value in [result.input_price, result.output_price]:
        if value:
            try:
                price = Decimal(value)
                if not price.is_finite() or price < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise GatewayError("MODEL_PRICE_INVALID") from None
    if not 0 <= settings.MODEL_MAX_RETRIES <= 3 or not 0 <= settings.MODEL_RETRY_MAX_DELAY_SECONDS <= 10:
        raise GatewayError("MODEL_RETRY_CONFIG_INVALID")
    if kind == "generation":
        if settings.GENERATION_TOKEN_PARAMETER not in {"max_tokens", "max_completion_tokens"}:
            raise GatewayError("MODEL_TOKEN_PARAMETER_INVALID")
        if (
            settings.GENERATION_MAX_OUTPUT_TOKENS < 32
            or settings.GENERATION_INPUT_BUDGET < 256
            or settings.GENERATION_INPUT_BUDGET + settings.GENERATION_MAX_OUTPUT_TOKENS
            > settings.GENERATION_CONTEXT_WINDOW
            or not 0 <= settings.GENERATION_HISTORY_BUDGET <= settings.GENERATION_INPUT_BUDGET
        ):
            raise GatewayError("MODEL_TOKEN_BUDGET_INVALID")
    elif (
        not 1 <= settings.EMBEDDING_DIMENSIONS <= 16000
        or not 1 <= settings.EMBEDDING_MAX_INPUT_TOKENS <= 1_000_000
    ):
        raise GatewayError("EMBEDDING_CONFIG_INVALID")
    return result

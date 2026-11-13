from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import BigIntegerField, Case, F, Sum, When
from django.utils import timezone
from rest_framework.exceptions import APIException

from ai_engine.gateway.budget import estimate_tokens
from apps.users.models import Tenant, User

from .models import ModelCallLog, UsageReservation


class QuotaExceeded(APIException):
    status_code = 429
    default_detail = "模型调用额度不足"
    default_code = "quota_exceeded"


def limits(tenant):
    config = tenant.quota_config
    return {
        "daily_tokens": config.get("daily_tokens", settings.DEFAULT_TENANT_DAILY_TOKENS),
        "user_daily_tokens": config.get("user_daily_tokens", settings.DEFAULT_USER_DAILY_TOKENS),
    }


def consumed(rows):
    return (
        rows.aggregate(
            total=Sum(
                Case(
                    When(status="reserved", then=F("reserved_tokens")),
                    default=F("charged_tokens"),
                    output_field=BigIntegerField(),
                )
            )
        )["total"]
        or 0
    )


@transaction.atomic
def reserve(ctx, amount, model, *, run=None, task=None):
    tenant = Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    if (
        not tenant.is_active
        or not User.objects.filter(pk=ctx.user.pk, tenant=tenant, is_active=True).exists()
    ):
        raise QuotaExceeded("账号或租户已停用")
    period = timezone.now().date()
    rows = UsageReservation.objects.filter(tenant=tenant, period=period)
    caps = limits(tenant)
    if run is not None and run.kind == "agent":
        from ai_engine.gateway.config import GatewayError

        budget = run.config_snapshot.get("agent", {}).get("token_budget", 0)
        if consumed(UsageReservation.objects.filter(run=run)) + amount > budget:
            raise GatewayError("AGENT_TOKEN_BUDGET_EXCEEDED")
    if (
        amount < 0
        or consumed(rows) + amount > caps["daily_tokens"]
        or consumed(rows.filter(user=ctx.user)) + amount > caps["user_daily_tokens"]
    ):
        raise QuotaExceeded()
    return UsageReservation.objects.create(
        tenant=tenant,
        user=ctx.user,
        period=period,
        reserved_tokens=amount,
        run=run,
        task=task,
        task_type=model.kind,
        model=model.model or "demo-extractive",
        trace_id=ctx.trace_id,
        expires_at=timezone.now() + timedelta(seconds=model.timeout + 60),
        price_snapshot=model.prices(),
    )


@transaction.atomic
def settle(
    reservation_id, *, usage=None, sent=False, rejected=False, local=False, estimate=0, status="completed"
):
    tenant_id = UsageReservation.objects.values_list("tenant_id", flat=True).get(pk=reservation_id)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    row = UsageReservation.objects.select_for_update().get(pk=reservation_id)
    if row.status != "reserved":
        return row
    if sent is None:
        sent = row.dispatched
    usage = dict(usage or {"source": "unknown"})
    actual = usage.get("total_tokens")
    known = usage.get("source") == "provider" and type(actual) is int and actual >= 0
    if known:
        charge, basis = actual, "provider"
    elif local and status == "completed":
        charge, basis = min(row.reserved_tokens, estimate), "local_estimate"
    elif sent and not rejected:
        charge, basis = row.reserved_tokens, "reserved_unknown"
    else:
        charge, basis = 0, "released"
    row.status = "released" if basis == "released" else "settled"
    row.charged_tokens = charge
    row.usage = {**usage, "quota_basis": basis, "quota_tokens": charge}
    row.finished_at = timezone.now()
    row.save()
    price = row.price_snapshot
    price["estimated_cost_usd"] = None
    if (
        known
        and price.get("input_per_million")
        and (
            row.task_type not in {"generation", "rewrite", "agent_planner"} or price.get("output_per_million")
        )
    ):
        inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens", 0)
        if type(inputs) is int and type(outputs) is int:
            cost = Decimal(price["input_per_million"]) * inputs
            if row.task_type in {"generation", "rewrite", "agent_planner"}:
                cost += Decimal(price["output_per_million"]) * outputs
            price["estimated_cost_usd"] = str(cost / 1_000_000)
    ModelCallLog.objects.create(
        tenant_id=row.tenant_id,
        reservation=row,
        run=row.run,
        task=row.task,
        trace_id=row.trace_id,
        task_type=row.task_type,
        model=row.model,
        status=status,
        usage=row.usage,
        price_snapshot=price,
    )
    return row


@transaction.atomic
def mark_dispatched(reservation_id):
    from ai_engine.gateway.config import GatewayError

    tenant_id = UsageReservation.objects.values_list("tenant_id", flat=True).get(pk=reservation_id)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    row = UsageReservation.objects.select_for_update().get(pk=reservation_id)
    if row.status != "reserved" or row.expires_at <= timezone.now():
        raise GatewayError("MODEL_RESERVATION_EXPIRED")
    row.dispatched = True
    row.save(update_fields=["dispatched"])


class CallMeter:
    def __init__(self, ctx, model, input_estimate, output_limit=0, *, run=None, task=None):
        self.ctx, self.model = ctx, model
        self.input_estimate, self.amount = input_estimate, input_estimate + output_limit
        self.run, self.task = run, task
        self.row = None
        self.sent = False

    def start(self):
        if self.ctx is not None:
            self.row = reserve(self.ctx, self.amount, self.model, run=self.run, task=self.task)

    def mark_sent(self):
        if self.row:
            mark_dispatched(self.row.pk)
        self.sent = True

    def finish(self, usage=None, text="", error=None):
        if not self.row:
            return
        if error is not None and hasattr(error, "code"):
            usage = {**(usage or {"source": "unknown"}), "error_code": error.code}
        settle(
            self.row.pk,
            usage=usage,
            sent=self.sent,
            rejected=getattr(error, "status", None) in {400, 401, 403, 404, 408, 413, 422, 429},
            local=self.model.provider == "demo",
            estimate=self.input_estimate + estimate_tokens(text),
            status="completed"
            if error is None
            else ("interrupted" if getattr(error, "code", "") == "MODEL_CANCELLED" else "failed"),
        )

    async def astart(self):
        await sync_to_async(self.start)()

    async def amark_sent(self):
        await sync_to_async(self.mark_sent)()

    async def afinish(self, usage=None, text="", error=None):
        await sync_to_async(self.finish)(usage, text, error)


def recover_stale_reservations():
    for pk in UsageReservation.objects.filter(status="reserved", expires_at__lt=timezone.now()).values_list(
        "id", flat=True
    )[:100]:
        # Read dispatched under the same lock as settlement, so a stale scan
        # cannot release a reservation that has already begun an upstream call.
        settle(pk, sent=None, status="interrupted")

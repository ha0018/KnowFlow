from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Tenant
from apps.users.permissions import require_capability
from common.context import RequestContext, audit

from .metering import consumed, limits
from .models import UsageReservation
from .probes import configuration_status, probe_model


class ModelsView(APIView):
    def get(self, request):
        require_capability(RequestContext.from_request(request), "monitoring.read")
        return Response(configuration_status())

    def post(self, request):
        ctx = RequestContext.from_request(request)
        require_capability(ctx, "monitoring.read")
        kind = serializers.ChoiceField(choices=["generation", "embedding"]).run_validation(
            request.data.get("kind")
        )
        return Response(probe_model(ctx, kind))


class QuotaInput(serializers.Serializer):
    daily_tokens = serializers.IntegerField(min_value=0, max_value=1_000_000_000)
    user_daily_tokens = serializers.IntegerField(min_value=0, max_value=1_000_000_000)


class QuotaView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        tenant = Tenant.objects.get(pk=ctx.tenant_id)
        caps = limits(tenant)
        rows = UsageReservation.objects.filter(tenant=tenant, period=timezone.now().date())
        mine = rows.filter(user=ctx.user)
        data = {
            "date": str(timezone.now().date()),
            "user_limit": caps["user_daily_tokens"],
            "user_committed": consumed(mine),
            "user_active_reservations": mine.filter(status="reserved").count(),
            "note": "额度包含预留和已结算；未知供应商用量保守计入预留值，不等于供应商账单。",
        }
        if ctx.user.is_tenant_admin:
            data.update(tenant_limit=caps["daily_tokens"], tenant_committed=consumed(rows), limits=caps)
        return Response(data)

    def put(self, request):
        if not request.user.is_tenant_admin:
            raise PermissionDenied("仅租户管理员可以设置额度")
        serializer = QuotaInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        ctx = RequestContext.from_request(request)
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
            tenant.quota_config = {**tenant.quota_config, **serializer.validated_data}
            tenant.save(update_fields=["quota_config"])
            audit(ctx, "quota.update", tenant)
        return self.get(request)

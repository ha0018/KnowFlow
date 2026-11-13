from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from common.context import RequestContext, audit
from common.serializers import StrictInput

from .models import Permission, Role, Tenant, User, UserRole
from .services import current_admin, update_member, update_role


class LoginThrottle(AnonRateThrottle):
    scope = "login"


class LoginSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        attrs["email"] = attrs["email"].lower()
        data = super().validate(attrs)
        if not self.user.tenant.is_active:
            raise PermissionDenied("租户已停用")
        data["user"] = UserSerializer(self.user).data
        return data


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_classes = [LoginThrottle]


class RefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        token = RefreshToken(attrs["refresh"])
        if not User.objects.filter(pk=token["user_id"], is_active=True, tenant__is_active=True).exists():
            raise PermissionDenied("账号或租户已停用")
        return super().validate(attrs)


class RefreshView(TokenRefreshView):
    serializer_class = RefreshSerializer


class UserSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    role_ids = serializers.SerializerMethodField()

    def get_role_ids(self, obj):
        return [str(role.pk) for role in obj.roles.all()]

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "display_name",
            "tenant_id",
            "tenant_name",
            "is_tenant_admin",
            "is_active",
            "role_ids",
        ]
        read_only_fields = fields


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class LogoutView(APIView):
    def post(self, request):
        value = request.data.get("refresh")
        if not isinstance(value, str):
            raise ValidationError("需要 refresh 凭据")
        try:
            token = RefreshToken(value)
            if str(token["user_id"]) != str(request.user.pk):
                raise PermissionDenied("凭据不属于当前用户")
            token.blacklist()
        except PermissionDenied:
            raise
        except Exception as exc:
            raise ValidationError("无效的刷新凭据") from exc
        return Response(status=status.HTTP_204_NO_CONTENT)


class MemberInput(StrictInput):
    email = serializers.EmailField()
    display_name = serializers.CharField(max_length=100, required=False, default="")
    password = serializers.CharField(write_only=True, min_length=8)


class MembersView(APIView):
    def check_admin(self, request):
        current_admin(RequestContext.from_request(request))

    def get(self, request):
        self.check_admin(request)
        return Response(
            UserSerializer(
                User.objects.filter(tenant=request.user.tenant)
                .select_related("tenant")
                .prefetch_related("roles"),
                many=True,
            ).data
        )

    def post(self, request):
        self.check_admin(request)
        serializer = MemberInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if User.objects.filter(email=data["email"].lower()).exists():
            raise ValidationError("该邮箱不可用")
        try:
            validate_password(data["password"], User(email=data["email"]))
        except DjangoValidationError as exc:
            raise ValidationError({"password": exc.messages}) from exc
        try:
            with transaction.atomic():
                Tenant.objects.select_for_update().get(pk=request.user.tenant_id)
                ctx = current_admin(RequestContext.from_request(request))
                user = User.objects.create_user(tenant_id=ctx.tenant_id, **data)
                audit(ctx, "member.create", user)
        except IntegrityError as exc:
            raise ValidationError("该邮箱不可用") from exc
        return Response(UserSerializer(user).data, status=201)


class RoleInput(StrictInput):
    code = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=100)
    permission_codes = serializers.ListField(child=serializers.CharField(), default=list)


class RolesView(MembersView):
    def get(self, request):
        self.check_admin(request)
        return Response(
            [
                {
                    "id": role.id,
                    "code": role.code,
                    "name": role.name,
                    "permission_codes": list(role.permissions.values_list("code", flat=True)),
                }
                for role in Role.objects.filter(tenant=request.user.tenant).prefetch_related("permissions")
            ]
        )

    def post(self, request):
        self.check_admin(request)
        serializer = RoleInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        codes = data.pop("permission_codes")
        perms = Permission.objects.filter(code__in=codes)
        if perms.count() != len(set(codes)):
            raise ValidationError("未知操作权限")
        if Role.objects.filter(tenant=request.user.tenant, code=data["code"]).exists():
            raise ValidationError("角色代码已存在")
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=request.user.tenant_id)
            current_admin(RequestContext.from_request(request))
            role = Role.objects.create(tenant=request.user.tenant, **data)
            role.permissions.set(perms)
            audit(RequestContext.from_request(request), "role.create", role)
        return Response({"id": role.id}, status=201)


class MemberRolesView(MembersView):
    http_method_names = ["put", "options"]

    def put(self, request, member_id):
        from django.shortcuts import get_object_or_404

        self.check_admin(request)
        member = get_object_or_404(User, pk=member_id, tenant=request.user.tenant)
        field = serializers.ListField(child=serializers.UUIDField())
        ids = field.run_validation(request.data.get("role_ids"))
        roles = list(Role.objects.filter(pk__in=ids, tenant=request.user.tenant))
        if len(roles) != len(set(ids)):
            raise ValidationError("角色不可用")
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=request.user.tenant_id)
            current_admin(RequestContext.from_request(request))
            UserRole.objects.filter(user=member).delete()
            UserRole.objects.bulk_create(
                [UserRole(tenant=request.user.tenant, user=member, role=r) for r in roles]
            )
            Tenant.objects.filter(pk=request.user.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
            audit(RequestContext.from_request(request), "member.roles", member)
        return Response(status=204)


class MemberUpdate(StrictInput):
    display_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)


class MemberDetailView(MembersView):
    http_method_names = ["patch", "options"]

    def patch(self, request, member_id):
        self.check_admin(request)
        data = MemberUpdate(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(
            UserSerializer(
                update_member(RequestContext.from_request(request), member_id, data.validated_data)
            ).data
        )


class RoleUpdate(StrictInput):
    name = serializers.CharField(max_length=100, required=False)
    permission_codes = serializers.ListField(child=serializers.CharField(), required=False)


class RoleDetailView(MembersView):
    http_method_names = ["patch", "options"]

    def patch(self, request, role_id):
        self.check_admin(request)
        data = RoleUpdate(data=request.data)
        data.is_valid(raise_exception=True)
        role = update_role(RequestContext.from_request(request), role_id, data.validated_data)
        return Response(
            {
                "id": role.pk,
                "name": role.name,
                "permission_codes": list(role.permissions.values_list("code", flat=True)),
            }
        )


class PermissionsView(MembersView):
    def get(self, request):
        self.check_admin(request)
        return Response(list(Permission.objects.values("code", "description")))


class AuthorizationSubjectsView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        if request.query_params.get("document_id"):
            from apps.documents.selectors import get_document

            get_document(
                ctx, serializers.UUIDField().run_validation(request.query_params["document_id"]), "manage"
            )
        elif request.query_params.get("knowledge_base_id"):
            from apps.knowledge.selectors import get_knowledge_base

            get_knowledge_base(
                ctx,
                serializers.UUIDField().run_validation(request.query_params["knowledge_base_id"]),
                "manage",
            )
        else:
            current_admin(ctx)
        return Response(
            {
                "members": list(
                    User.objects.filter(tenant_id=ctx.tenant_id).values(
                        "id", "email", "display_name", "is_active"
                    )
                ),
                "roles": list(Role.objects.filter(tenant_id=ctx.tenant_id).values("id", "code", "name")),
            }
        )

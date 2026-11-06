import hashlib
import json
from datetime import datetime, timezone

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.agents.models import BusinessIssue
from apps.documents.services import upload
from apps.evaluation.models import EvalCase, EvalDataset
from apps.knowledge.models import KnowledgeBase
from apps.users.models import Permission, Role, Tenant, User, UserRole
from common.context import RequestContext

CORPUS = [
    (
        "退款政策",
        "# 退款政策\n\n标准产品在购买后 30 天内可以申请退款。超过 30 天原则上不支持退款；如因产品质量问题，可联系人工客服审核。\n\n申请退款需提供订单编号，审核时间为 3 个工作日。",
    ),
    (
        "产品使用手册",
        "# KnowFlow 产品使用手册\n\n上传文档支持 PDF、DOCX、TXT 和 Markdown。单文件最大 20 MB。扫描 PDF 需要 OCR，当前版本不支持。\n\n成员仅能检索有权限的知识库。私有知识库需要管理员授权。",
    ),
    (
        "员工休假制度",
        "# 员工休假制度\n\n员工每年享有 10 天带薪年假。年假申请至少提前 3 个工作日提交给直属主管审批。\n\n病假需提供有效医疗证明，紧急情况可以在返岗后补交。",
    ),
    (
        "账号安全指南",
        "# 账号安全指南\n\n密码不得在团队间共享。离职成员需由管理员立即停用账号并撤销知识库授权。\n\n访问令牌有效期为 15 分钟；刷新令牌轮换后旧凭据失效。",
    ),
]
QUESTIONS = [
    (0, "退款超过30天还能申请吗？"),
    (0, "退款审核需要几天？"),
    (0, "申请退款需要什么资料？"),
    (0, "购买后多少天内可以退款？"),
    (0, "产品质量问题如何退款？"),
    (1, "支持上传哪些文档？"),
    (1, "上传文件最大多大？"),
    (1, "扫描PDF支持吗？"),
    (1, "私有知识库如何访问？"),
    (1, "成员可以检索哪些知识库？"),
    (2, "员工每年有几天年假？"),
    (2, "年假提前几天申请？"),
    (2, "病假需要什么证明？"),
    (2, "紧急病假能否返岗后补交？"),
    (2, "年假由谁审批？"),
    (3, "访问令牌有效期多久？"),
    (3, "离职成员如何处理？"),
    (3, "可以共享密码吗？"),
    (3, "刷新令牌轮换后旧凭据会怎样？"),
    (3, "账号安全指南对密码有什么要求？"),
]


class Command(BaseCommand):
    help = "Create two isolated demo tenants, sample documents and 20 development evaluation cases"

    def add_arguments(self, parser):
        parser.add_argument(
            "--password", required=True, help="Password for newly created demo users (8+ characters)"
        )

    def handle(self, *args, **options):
        password = options["password"]
        if len(password) < 8:
            raise CommandError("Password must contain at least 8 characters")
        for slug, name in [("acme", "星河科技"), ("atlas", "远山实验室")]:
            tenant, _ = Tenant.objects.get_or_create(slug=slug, defaults={"name": name})
            with transaction.atomic():
                admin, created = User.objects.get_or_create(
                    email=f"admin@{slug}.demo",
                    defaults={"tenant": tenant, "display_name": "演示管理员", "is_tenant_admin": True},
                )
                if created:
                    admin.set_password(password)
                    admin.save()
                member, created = User.objects.get_or_create(
                    email=f"member@{slug}.demo", defaults={"tenant": tenant, "display_name": "知识库成员"}
                )
                if created:
                    member.set_password(password)
                    member.save()
                role, _ = Role.objects.get_or_create(
                    tenant=tenant, code="analyst", defaults={"name": "知识分析员"}
                )
                for code in ["knowledge.create", "agent.execute", "evaluation.run", "monitoring.read"]:
                    permission, _ = Permission.objects.get_or_create(
                        code=code, defaults={"description": code}
                    )
                    if code in {"knowledge.create", "agent.execute"}:
                        role.permissions.add(permission)
                UserRole.objects.get_or_create(tenant=tenant, user=member, role=role)
                kb, _ = KnowledgeBase.objects.get_or_create(
                    tenant=tenant,
                    name="企业知识中心",
                    defaults={
                        "owner": admin,
                        "visibility": "tenant",
                        "description": "产品、制度与服务规范的统一入口",
                    },
                )
            ctx = RequestContext.for_user(admin)
            docs = []
            for title, text in CORPUS:
                existing = kb.documents.filter(title=title).first()
                if not existing:
                    existing, _, _, _ = upload(
                        ctx,
                        kb.pk,
                        SimpleUploadedFile(f"{title}.md", text.encode(), content_type="text/markdown"),
                        title,
                    )
                docs.append(existing)
            manifest = hashlib.sha256(json.dumps(CORPUS, ensure_ascii=False).encode()).hexdigest()
            dataset, created = EvalDataset.objects.get_or_create(
                tenant=tenant,
                name="演示召回开发集",
                version="v1",
                defaults={"corpus_manifest_hash": manifest, "split": "development"},
            )
            if created:
                for index, question in QUESTIONS:
                    version = docs[index].versions.order_by("version_no").first()
                    EvalCase.objects.create(
                        tenant=tenant,
                        dataset=dataset,
                        question=question,
                        gold_evidence=[
                            {
                                "document_id": str(docs[index].pk),
                                "version_id": str(version.pk),
                                "sha256": version.sha256,
                                "evidence": CORPUS[index][1],
                            }
                        ],
                    )
            if not BusinessIssue.objects.filter(tenant=tenant).exists():
                BusinessIssue.objects.bulk_create(
                    [
                        BusinessIssue(
                            tenant=tenant,
                            occurred_at=datetime(2026, (i % 6) + 1, (i % 25) + 1, tzinfo=timezone.utc),
                            product=["KnowFlow", "FlowDesk"][i % 2],
                            category=["使用咨询", "缺陷反馈", "功能建议"][i % 3],
                            description="自编演示样本",
                        )
                        for i in range(36)
                    ]
                )
            self.stdout.write(self.style.SUCCESS(f"{name}: admin@{slug}.demo / member@{slug}.demo"))
        self.stdout.write(
            "Existing user passwords are unchanged. Demo accounts cannot access Django's operator admin."
        )

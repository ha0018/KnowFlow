from django.db import migrations


def log_guards(schema_editor, include_reservation):
    links = [("run_id", "conversations_run"), ("task_id", "documents_documenttask")]
    if include_reservation:
        links.append(("reservation_id", "monitoring_usagereservation"))
    checks = [
        f"(NEW.{col} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {parent} WHERE id=NEW.{col} AND tenant_id=NEW.tenant_id))"
        for col, parent in links
    ]
    for operation in ["INSERT", "UPDATE"]:
        name = "monitoring_modelcalllog_scope_" + operation.lower()
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {name}")
        condition = " OR ".join(checks)
        if operation == "UPDATE":
            condition += " OR NEW.tenant_id IS NOT OLD.tenant_id"
        schema_editor.execute(
            f"CREATE TRIGGER {name} BEFORE {operation} ON monitoring_modelcalllog WHEN {condition} "
            "BEGIN SELECT RAISE(ABORT,'model call tenant mismatch'); END"
        )


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "ALTER TABLE monitoring_usagereservation ADD CONSTRAINT usage_tenant_identity UNIQUE (tenant_id,id)"
        )
        for column, parent in [
            ("user_id", "users_user"),
            ("run_id", "conversations_run"),
            ("task_id", "documents_documenttask"),
        ]:
            schema_editor.execute(
                f"ALTER TABLE monitoring_usagereservation ADD CONSTRAINT usage_{column}_scope "
                f"FOREIGN KEY (tenant_id,{column}) REFERENCES {parent}(tenant_id,id) DEFERRABLE INITIALLY DEFERRED"
            )
        schema_editor.execute(
            "ALTER TABLE monitoring_modelcalllog ADD CONSTRAINT call_reservation_scope "
            "FOREIGN KEY(tenant_id,reservation_id) REFERENCES monitoring_usagereservation(tenant_id,id) DEFERRABLE INITIALLY DEFERRED"
        )
        schema_editor.execute("""CREATE FUNCTION usage_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP='UPDATE' AND (NEW.tenant_id, NEW.user_id, NEW.run_id, NEW.task_id, NEW.period, NEW.reserved_tokens)
                    IS DISTINCT FROM (OLD.tenant_id, OLD.user_id, OLD.run_id, OLD.task_id, OLD.period, OLD.reserved_tokens) THEN
                    RAISE EXCEPTION 'usage identity is immutable' USING ERRCODE='23514';
                END IF;
                IF NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM conversations_run r WHERE r.id=NEW.run_id AND r.user_id=NEW.user_id) THEN
                    RAISE EXCEPTION 'usage run actor mismatch' USING ERRCODE='23514';
                END IF;
                IF NEW.run_id IS NOT NULL AND NEW.task_id IS NOT NULL THEN
                    RAISE EXCEPTION 'usage has multiple parents' USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END; $$""")
        schema_editor.execute(
            "CREATE TRIGGER usage_guard BEFORE INSERT OR UPDATE ON monitoring_usagereservation FOR EACH ROW EXECUTE FUNCTION usage_guard()"
        )
    elif schema_editor.connection.vendor == "sqlite":
        checks = []
        for column, parent in [
            ("user_id", "users_user"),
            ("run_id", "conversations_run"),
            ("task_id", "documents_documenttask"),
        ]:
            checks.append(
                f"(NEW.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {parent} WHERE id=NEW.{column} AND tenant_id=NEW.tenant_id))"
            )
        checks.append(
            "(NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM conversations_run WHERE id=NEW.run_id AND user_id=NEW.user_id))"
        )
        checks.append("(NEW.run_id IS NOT NULL AND NEW.task_id IS NOT NULL)")
        for operation in ["INSERT", "UPDATE"]:
            condition = " OR ".join(checks)
            if operation == "UPDATE":
                condition += " OR " + " OR ".join(
                    f"NEW.{c} IS NOT OLD.{c}"
                    for c in ["tenant_id", "user_id", "run_id", "task_id", "period", "reserved_tokens"]
                )
            schema_editor.execute(
                f"CREATE TRIGGER usage_scope_{operation.lower()} BEFORE {operation} ON monitoring_usagereservation WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT,'usage identity or tenant mismatch'); END"
            )
        # Adding a unique FK can rebuild this table on SQLite and drop the
        # original guards. Restore all scopes, including the newly added FK.
        log_guards(schema_editor, True)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER usage_guard ON monitoring_usagereservation")
        schema_editor.execute("DROP FUNCTION usage_guard()")
        schema_editor.execute("ALTER TABLE monitoring_modelcalllog DROP CONSTRAINT call_reservation_scope")
        for column in ["user_id", "run_id", "task_id"]:
            schema_editor.execute(
                f"ALTER TABLE monitoring_usagereservation DROP CONSTRAINT usage_{column}_scope"
            )
        schema_editor.execute("ALTER TABLE monitoring_usagereservation DROP CONSTRAINT usage_tenant_identity")
    else:
        for name in [
            "usage_scope_insert",
            "usage_scope_update",
            "call_reservation_scope_insert",
            "call_reservation_scope_update",
        ]:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name}")
        log_guards(schema_editor, False)


class Migration(migrations.Migration):
    dependencies = [("monitoring", "0005_usagereservation_modelcalllog_reservation_and_more")]
    operations = [migrations.RunPython(install, uninstall)]

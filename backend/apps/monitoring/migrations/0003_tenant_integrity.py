"""Database-level isolation for every tenant-owned FK and document/build relation.

SQLite uses triggers for the portable demonstration. PostgreSQL uses composite
foreign keys plus relation triggers. Parent tenant ownership is immutable.
"""

from django.db import migrations

EXTRA_CHECKS = {
    "documents.Document": [
        (
            "active_build_id",
            "documents_indexbuild",
            "version_id",
            "documents_documentversion",
            "document_id",
            "id",
        ),
        (
            "desired_build_id",
            "documents_indexbuild",
            "version_id",
            "documents_documentversion",
            "document_id",
            "id",
        ),
    ],
    "documents.DocumentChunk": [
        ("document_id", "documents_document", "knowledge_base_id", None, None, "knowledge_base_id"),
        (
            "build_id",
            "documents_indexbuild",
            "version_id",
            "documents_documentversion",
            "document_id",
            "document_id",
        ),
    ],
    "conversations.Run": [
        ("input_message_id", "conversations_message", "conversation_id", None, None, "conversation_id"),
        ("output_message_id", "conversations_message", "conversation_id", None, None, "conversation_id"),
        ("conversation_id", "conversations_conversation", "user_id", None, None, "user_id"),
    ],
    "evaluation.EvalResult": [
        ("case_id", "evaluation_evalcase", "dataset_id", None, None, "run_id:dataset_id"),
    ],
}


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    quote = schema_editor.quote_name
    owned = [m for m in apps.get_models() if any(f.name == "tenant" for f in m._meta.fields)]
    if vendor == "postgresql":
        for model in owned:
            table = model._meta.db_table
            schema_editor.execute(
                f"ALTER TABLE {quote(table)} ADD CONSTRAINT {quote(table + '_tenant_identity')} UNIQUE (tenant_id, id)"
            )
    for model in owned:
        table = model._meta.db_table
        checks = []
        for field in model._meta.fields:
            if not field.is_relation or not field.many_to_one or field.name == "tenant":
                continue
            parent = field.remote_field.model
            if parent not in owned:
                continue
            target = parent._meta.db_table
            col = field.column
            if vendor == "postgresql":
                schema_editor.execute(
                    f"ALTER TABLE {quote(table)} ADD CONSTRAINT {quote(table + '_' + field.name + '_scope')} "
                    f"FOREIGN KEY (tenant_id, {quote(col)}) REFERENCES {quote(target)} (tenant_id, id) DEFERRABLE INITIALLY DEFERRED"
                )
            else:
                checks.append(
                    f"NEW.{quote(col)} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {quote(target)} WHERE id = NEW.{quote(col)} AND tenant_id = NEW.tenant_id)"
                )
        for fk, parent, value, via, via_value, local in EXTRA_CHECKS.get(model._meta.label, []):
            query = f"SELECT p.{quote(value)} FROM {quote(parent)} p WHERE p.id = NEW.{quote(fk)}"
            if via:
                query = f"SELECT v.{quote(via_value)} FROM {quote(parent)} p JOIN {quote(via)} v ON v.id=p.{quote(value)} WHERE p.id=NEW.{quote(fk)}"
            rhs = f"NEW.{quote(local)}"
            if ":" in local:
                col, attr = local.split(":")
                rhs = f"(SELECT {quote(attr)} FROM evaluation_evalrun WHERE id=NEW.{quote(col)})"
            checks.append(f"NEW.{quote(fk)} IS NOT NULL AND ({query}) <> {rhs}")
        if vendor == "sqlite":
            for operation in ("INSERT", "UPDATE"):
                condition = " OR ".join(f"({check})" for check in checks) or "0"
                if operation == "UPDATE":
                    condition += " OR NEW.tenant_id <> OLD.tenant_id"
                schema_editor.execute(
                    f"CREATE TRIGGER {quote(table + '_scope_' + operation.lower())} BEFORE {operation} ON {quote(table)} "
                    f"WHEN {condition} BEGIN SELECT RAISE(ABORT, 'tenant or resource relation mismatch'); END"
                )
        elif vendor == "postgresql":
            condition = " OR ".join(f"({check})" for check in checks) or "FALSE"
            function = quote(table + "_scope_guard")
            schema_editor.execute(f"""CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP = 'UPDATE' AND NEW.tenant_id <> OLD.tenant_id THEN
                        RAISE EXCEPTION 'tenant ownership is immutable' USING ERRCODE = '23514';
                    END IF;
                    IF {condition} THEN
                        RAISE EXCEPTION 'resource relation mismatch' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END; $$""")
            schema_editor.execute(
                f"CREATE TRIGGER {quote(table + '_scope')} BEFORE INSERT OR UPDATE ON {quote(table)} "
                f"FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
    if vendor == "postgresql":
        schema_editor.execute(
            "CREATE INDEX chunk_search_gin ON documents_documentchunk USING gin(search_vector)"
        )


def uninstall(apps, schema_editor):
    quote = schema_editor.quote_name
    owned = [m for m in apps.get_models() if any(f.name == "tenant" for f in m._meta.fields)]
    for model in owned:
        table = model._meta.db_table
        if schema_editor.connection.vendor == "sqlite":
            for operation in ("insert", "update"):
                schema_editor.execute(f"DROP TRIGGER IF EXISTS {quote(table + '_scope_' + operation)}")
        else:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {quote(table + '_scope')} ON {quote(table)}")
            schema_editor.execute(f"DROP FUNCTION IF EXISTS {quote(table + '_scope_guard')}()")
            for field in model._meta.fields:
                if (
                    field.is_relation
                    and field.many_to_one
                    and field.name != "tenant"
                    and field.remote_field.model in owned
                ):
                    schema_editor.execute(
                        f"ALTER TABLE {quote(table)} DROP CONSTRAINT IF EXISTS {quote(table + '_' + field.name + '_scope')}"
                    )
    if schema_editor.connection.vendor == "postgresql":
        for model in owned:
            table = model._meta.db_table
            schema_editor.execute(
                f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(table + '_tenant_identity')}"
            )
        schema_editor.execute("DROP INDEX IF EXISTS chunk_search_gin")


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0001_initial"),
        ("knowledge", "0002_initial"),
        ("documents", "0003_initial"),
        ("conversations", "0002_initial"),
        ("agents", "0002_initial"),
        ("evaluation", "0002_initial"),
        ("monitoring", "0002_initial"),
    ]
    operations = [migrations.RunPython(install, uninstall)]

from django.db import migrations

# These fields define immutable provenance. Mutable build pointers are excluded.
FROZEN = {
    "documents_document": ["knowledge_base_id"],
    "documents_documentversion": [
        "document_id",
        "version_no",
        "file",
        "original_name",
        "sha256",
        "size_bytes",
        "mime_type",
        "created_by_id",
    ],
    "documents_indexbuild": ["version_id", "pipeline_hash", "embedding_profile", "config_snapshot"],
    "documents_documentchunk": [
        "document_id",
        "build_id",
        "knowledge_base_id",
        "ordinal",
        "content",
        "content_hash",
        "locator",
    ],
    "conversations_conversation": ["user_id"],
    "conversations_message": ["conversation_id", "seq", "role"],
    "conversations_run": ["conversation_id", "user_id", "input_message_id", "output_message_id"],
    "evaluation_evalcase": ["dataset_id", "question", "gold_evidence", "category"],
    "evaluation_evalresult": ["run_id", "case_id"],
}


def install(apps, schema_editor):
    quote = schema_editor.quote_name
    for table, columns in FROZEN.items():
        name = quote(table + "_immutable")
        if schema_editor.connection.vendor == "sqlite":
            condition = " OR ".join(f"NEW.{quote(col)} IS NOT OLD.{quote(col)}" for col in columns)
            schema_editor.execute(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON {quote(table)} WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'resource provenance is immutable'); END"
            )
        elif schema_editor.connection.vendor == "postgresql":
            condition = " OR ".join(f"NEW.{quote(col)} IS DISTINCT FROM OLD.{quote(col)}" for col in columns)
            schema_editor.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF {condition} THEN RAISE EXCEPTION 'resource provenance is immutable' USING ERRCODE='23514';
                END IF; RETURN NEW; END; $$""")
            schema_editor.execute(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON {quote(table)} FOR EACH ROW EXECUTE FUNCTION {name}()"
            )


def uninstall(apps, schema_editor):
    quote = schema_editor.quote_name
    for table in FROZEN:
        name = quote(table + "_immutable")
        if schema_editor.connection.vendor == "sqlite":
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name}")
        else:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name} ON {quote(table)}")
            schema_editor.execute(f"DROP FUNCTION IF EXISTS {name}()")


class Migration(migrations.Migration):
    dependencies = [("monitoring", "0003_tenant_integrity")]
    operations = [migrations.RunPython(install, uninstall)]

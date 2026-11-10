from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

INDEX_NAME = "documents_embedding_hnsw_cosine"


class Command(BaseCommand):
    help = "Inspect or explicitly create the optional cosine HNSW index with the migration account"

    def add_arguments(self, parser):
        parser.add_argument("--create", action="store_true")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql" or settings.EMBEDDING_DIMENSIONS > 2000:
            raise CommandError("HNSW requires PostgreSQL and a vector dimension <= 2000")
        with connection.cursor() as cursor:
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            version = cursor.fetchone()
            if not version or tuple(map(int, version[0].split(".")[:2])) < (0, 8):
                raise CommandError("pgvector >= 0.8 is required for filtered iterative scans")
            if options["create"]:
                cursor.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON documents_documentchunk USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
                )
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=%s",
                [INDEX_NAME],
            )
            row = cursor.fetchone()
            self.stdout.write(
                row[0] if row else "Optional HNSW index is absent; exact retrieval remains available"
            )

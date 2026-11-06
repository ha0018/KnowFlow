#!/bin/sh
set -eu

# Runs on initial volume creation. It can also be rerun explicitly for an
# existing deployment; no tables or existing tenant data are removed.
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set app_password="$POSTGRES_APP_PASSWORD" --set database="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE knowflow_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'knowflow_app') \gexec
ALTER ROLE knowflow_app PASSWORD :'app_password';
ALTER ROLE knowflow_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO knowflow_app;
GRANT USAGE ON SCHEMA public TO knowflow_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO knowflow_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO knowflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO knowflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO knowflow_app;
SQL

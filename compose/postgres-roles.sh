#!/bin/sh
# Runs once, when the postgres image initialises an empty data volume, as the superuser.
# Creates the two application login roles and the read-only group they belong to.
# Passwords come from Compose secret files and reach SQL only as psql variables,
# never through string interpolation. sql/schema.sql (applied by the loader) grants
# the table privileges to preflight_reader.
set -eu

psql -X -q -v ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER:-postgres}" --dbname "${POSTGRES_DB:-postgres}" <<'SQL'
\set api_password `cat /run/secrets/api-password`
\set mcp_password `cat /run/secrets/mcp-password`
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'preflight_reader') THEN
        CREATE ROLE preflight_reader NOLOGIN;
    END IF;
END $$;
CREATE ROLE preflight_oss_api LOGIN PASSWORD :'api_password' IN ROLE preflight_reader;
CREATE ROLE preflight_oss_mcp LOGIN PASSWORD :'mcp_password' IN ROLE preflight_reader;
SQL

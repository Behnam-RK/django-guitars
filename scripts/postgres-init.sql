-- Provision the application role for the dev/test database.
--
-- Runs once, from /docker-entrypoint-initdb.d/, as the image's superuser
-- (POSTGRES_USER=postgres) against an empty data directory.
--
-- Why this file exists at all: the kit's tenancy feature is enforced by
-- PostgreSQL row-level-security policies, and RLS has three silent bypasses.
--
--   1. A SUPERUSER bypasses RLS unconditionally -- even a policy created with
--      FORCE ROW LEVEL SECURITY. Rows come back unfiltered, with no error and no
--      log line.
--   2. The BYPASSRLS role attribute does the same, and is granted separately from
--      SUPERUSER, so ruling out the one does not rule out the other.
--   3. A table's OWNER bypasses RLS unless the table is FORCE'd. That is the
--      exact condition FORCE exists to close, so it has to be the condition the
--      test suite runs under.
--
-- Before this file, compose made `guitars` the POSTGRES_USER and therefore a
-- superuser, so every RLS assertion in the suite would have passed vacuously.
-- The role below is deliberately NOT a superuser.
--
-- CREATEDB is required because pytest-django creates `test_guitars` itself. That
-- also makes this role the owner of every table in it -- case 2 above -- which is
-- what lets the FORCE tests prove something.
--
-- Connection parameters are unchanged from before (guitars/guitars@guitars), so
-- core/settings.py needs no corresponding edit.

CREATE ROLE guitars LOGIN CREATEDB PASSWORD 'guitars';

CREATE DATABASE guitars OWNER guitars;

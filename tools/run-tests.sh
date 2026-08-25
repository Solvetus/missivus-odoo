#!/usr/bin/env sh
# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
#
# Runs the missivus_mail_graph test suite with Odoo's own runner on a fresh database.
# Env: DB_HOST (default db), DB_USER/DB_PASSWORD (default odoo), DB_NAME (default missivus_test),
#      ADDONS_DIR (default: repo root).
# Exit status is Odoo's: non-zero when any test fails or errors.
set -eu
ADDONS_DIR="${ADDONS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DB_NAME="${DB_NAME:-missivus_test}"
DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-odoo}"
DB_PASSWORD="${DB_PASSWORD:-odoo}"
export PGPASSWORD="$DB_PASSWORD"
# A running dev server may still hold sessions on the test database: end them, then drop it.
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -q -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();" >/dev/null
dropdb --if-exists -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"
exec odoo \
  -d "$DB_NAME" \
  --db_host="$DB_HOST" --db_port="$DB_PORT" \
  --db_user="$DB_USER" --db_password="$DB_PASSWORD" \
  --addons-path="/usr/lib/python3/dist-packages/odoo/addons,${ADDONS_DIR}" \
  --data-dir=/tmp/odoo-data \
  -i missivus_mail_graph --test-enable --test-tags /missivus_mail_graph \
  --stop-after-init --log-level=test "$@"

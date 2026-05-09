#!/bin/bash
# List all service databases and roles in PostgreSQL.
#
# Usage:
#   ./list_service_dbs.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load admin credentials
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    echo "Error: ${SCRIPT_DIR}/.env not found. Cannot read admin credentials."
    exit 1
fi

source "${SCRIPT_DIR}/.env"

# Determine the container name
CONTAINER_NAME=$(sudo docker ps --filter "name=postgres-postgres" --format '{{.Names}}' | head -n 1)
if [[ -z "$CONTAINER_NAME" ]]; then
    echo "Error: No running postgres container found matching 'postgres-postgres'."
    exit 1
fi

echo "=== Service Roles ==="
sudo docker exec -t "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d postgres -c \
    "SELECT rolname AS role, rolsuper AS superuser, rolcanlogin AS can_login
     FROM pg_roles
     WHERE rolname NOT LIKE 'pg_%'
     ORDER BY rolname;"

echo ""
echo "=== Databases ==="
sudo docker exec -t "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d postgres -c \
    "SELECT d.datname AS database,
            r.rolname AS owner,
            pg_size_pretty(pg_database_size(d.datname)) AS size
     FROM pg_database d
     JOIN pg_roles r ON d.datdba = r.oid
     WHERE d.datname NOT IN ('template0', 'template1')
     ORDER BY d.datname;"

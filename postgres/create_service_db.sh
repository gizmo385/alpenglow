#!/bin/bash
# Create a new PostgreSQL database and role for a service.
#
# This follows the established pattern: each service gets a dedicated role
# (NOSUPERUSER, LOGIN only) and a database of the same name owned by that role.
#
# Usage:
#   ./create_service_db.sh <service_name>
#   ./create_service_db.sh <service_name> <db_name>    # if db name differs from role
#
# The script connects to the postgres container via docker exec using the
# admin credentials from the postgres .env file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load admin credentials
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    echo "Error: ${SCRIPT_DIR}/.env not found. Cannot read admin credentials."
    exit 1
fi

source "${SCRIPT_DIR}/.env"

SERVICE_NAME="${1:?Usage: $0 <service_name> [db_name]}"
DB_NAME="${2:-$SERVICE_NAME}"

# Determine the container name by checking for running postgres containers
CONTAINER_NAME=$(sudo docker ps --filter "name=postgres-postgres" --format '{{.Names}}' | head -n 1)
if [[ -z "$CONTAINER_NAME" ]]; then
    echo "Error: No running postgres container found matching 'postgres-postgres'."
    exit 1
fi

# Generate a random password (44 chars, base64-encoded from 33 bytes)
PASSWORD=$(openssl rand -base64 33)

echo "Creating role '${SERVICE_NAME}' and database '${DB_NAME}'..."
echo "Container: ${CONTAINER_NAME}"
echo ""

# Create the role and database
sudo docker exec -i "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d postgres <<SQL
-- Create the service role
CREATE ROLE ${SERVICE_NAME} WITH
    NOSUPERUSER
    INHERIT
    NOCREATEROLE
    NOCREATEDB
    LOGIN
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD '${PASSWORD}';

-- Create the database owned by the service role
CREATE DATABASE ${DB_NAME}
    WITH OWNER = ${SERVICE_NAME}
    ENCODING = 'UTF8'
    LOCALE_PROVIDER = libc
    TEMPLATE = template0;
SQL

echo ""
echo "Done! Service database setup complete."
echo ""
echo "Connection details:"
echo "  Host:     postgres.postgres"
echo "  Port:     5432"
echo "  Database: ${DB_NAME}"
echo "  Username: ${SERVICE_NAME}"
echo "  Password: ${PASSWORD}"
echo ""
echo "  URL: postgresql://${SERVICE_NAME}:${PASSWORD}@postgres.postgres:5432/${DB_NAME}"
echo ""
echo "Add these to your service's .env file as appropriate."

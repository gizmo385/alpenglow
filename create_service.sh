#!/bin/bash
# Create a new service directory with boilerplate files and an optional system user account.
#
# This follows the established patterns in this repo:
#   - A service directory with compose.yaml, .env, .env.example, .gitignore
#   - An optional dedicated system user (nologin, /nonexistent home) for running the container
#   - An optional dedicated PostgreSQL database and role
#
# Usage:
#   ./create_service.sh <service_name> [options]
#
# Options:
#   --user              Create a system user account for the service
#   --postgres          Create a PostgreSQL database and role for the service
#   --network <name>    Add an external network to compose.yaml (repeatable)
#   --image <image>     Set the container image in compose.yaml
#   --caddy <subdomain> Add caddy reverse proxy labels (e.g. --caddy recipes)
#   --port <port>       Container port to reverse proxy to (default: 8080)
#
# Examples:
#   ./create_service.sh myapp --image ghcr.io/org/myapp:latest --user --postgres --network caddy --caddy myapp

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Argument parsing ---
SERVICE_NAME=""
CREATE_USER=false
CREATE_POSTGRES=false
NETWORKS=()
IMAGE=""
CADDY_SUBDOMAIN=""
CONTAINER_PORT="8080"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            CREATE_USER=true
            shift
            ;;
        --postgres)
            CREATE_POSTGRES=true
            shift
            ;;
        --network)
            NETWORKS+=("$2")
            shift 2
            ;;
        --image)
            IMAGE="$2"
            shift 2
            ;;
        --caddy)
            CADDY_SUBDOMAIN="$2"
            shift 2
            ;;
        --port)
            CONTAINER_PORT="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#$//; p }' "$0"
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [[ -z "$SERVICE_NAME" ]]; then
                SERVICE_NAME="$1"
            else
                echo "Unexpected argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$SERVICE_NAME" ]]; then
    echo "Usage: $0 <service_name> [options]"
    echo "Run with --help for full usage."
    exit 1
fi

SERVICE_DIR="${SCRIPT_DIR}/${SERVICE_NAME}"

if [[ -d "$SERVICE_DIR" ]]; then
    echo "Error: Directory ${SERVICE_DIR} already exists."
    exit 1
fi

# --- Create system user ---
SERVICE_UID=""
SERVICE_GID=""

if $CREATE_USER; then
    if id "$SERVICE_NAME" &>/dev/null; then
        echo "System user '${SERVICE_NAME}' already exists."
        SERVICE_UID=$(id -u "$SERVICE_NAME")
        SERVICE_GID=$(id -g "$SERVICE_NAME")
    else
        echo "Creating system user '${SERVICE_NAME}'..."
        sudo useradd \
            --system \
            --no-create-home \
            --home-dir /nonexistent \
            --shell /usr/sbin/nologin \
            "$SERVICE_NAME"
        SERVICE_UID=$(id -u "$SERVICE_NAME")
        SERVICE_GID=$(id -g "$SERVICE_NAME")
        echo "Created user ${SERVICE_NAME} (${SERVICE_UID}:${SERVICE_GID})"
    fi
fi

# --- Create service directory ---
echo "Creating service directory: ${SERVICE_DIR}"
mkdir -p "$SERVICE_DIR"

# --- Build compose.yaml ---
{
    echo "services:"
    echo "  ${SERVICE_NAME}:"
    if [[ -n "$IMAGE" ]]; then
        echo "    image: ${IMAGE}"
    else
        echo "    image: TODO"
    fi
    echo "    restart: unless-stopped"
    echo "    env_file: .env"

    if [[ -n "$SERVICE_UID" ]]; then
        echo "    user: \"${SERVICE_UID}:${SERVICE_GID}\""
    fi

    # Caddy labels
    if [[ -n "$CADDY_SUBDOMAIN" ]]; then
        echo "    labels:"
        echo "      caddy: \"*.acbc.house\""
        echo "      caddy.@${SERVICE_NAME}.host: \"${CADDY_SUBDOMAIN}.acbc.house\""
        echo "      caddy.reverse_proxy: \"@${SERVICE_NAME} {{upstreams http ${CONTAINER_PORT}}}\""
    fi

    # Networks on the service
    if [[ ${#NETWORKS[@]} -gt 0 ]]; then
        echo "    networks:"
        for net in "${NETWORKS[@]}"; do
            echo "      - ${net}"
        done
    fi

    echo ""

    # Top-level networks block
    if [[ ${#NETWORKS[@]} -gt 0 ]]; then
        echo "networks:"
        for net in "${NETWORKS[@]}"; do
            echo "  ${net}:"
            echo "    external: true"
        done
    fi
} > "${SERVICE_DIR}/compose.yaml"

# --- Create .env and .gitignore ---
touch "${SERVICE_DIR}/.env"
echo "data" > "${SERVICE_DIR}/.gitignore"

# --- Set group ownership to service-owners ---
sudo chgrp -R service-owners "${SERVICE_DIR}"
sudo chmod -R g+rw "${SERVICE_DIR}"

# --- Create PostgreSQL database ---
if $CREATE_POSTGRES; then
    echo ""
    if [[ -x "${SCRIPT_DIR}/postgres/create_service_db.sh" ]]; then
        "${SCRIPT_DIR}/postgres/create_service_db.sh" "$SERVICE_NAME"
    else
        echo "Warning: postgres/create_service_db.sh not found or not executable."
        echo "You will need to create the database manually."
    fi
fi

# --- Generate .env.example ---
if [[ -x "${SCRIPT_DIR}/generate-env-stubs.sh" ]]; then
    "${SCRIPT_DIR}/generate-env-stubs.sh" 2>/dev/null | grep -q "${SERVICE_NAME}" || true
fi

# --- Summary ---
echo ""
echo "=== Service '${SERVICE_NAME}' created ==="
echo ""
echo "Directory: ${SERVICE_DIR}"
echo "Files:"
echo "  compose.yaml  - Docker Compose config (review and customize)"
echo "  .env          - Environment variables (add secrets here)"
echo "  .gitignore    - Ignores data directory"
echo "  data/         - Persistent data volume"
if [[ -n "$SERVICE_UID" ]]; then
    echo ""
    echo "System user: ${SERVICE_NAME} (${SERVICE_UID}:${SERVICE_GID})"
    echo "  data/ ownership set to ${SERVICE_UID}:${SERVICE_GID}"
fi
echo ""
echo "Next steps:"
echo "  1. Edit ${SERVICE_DIR}/compose.yaml to finalize the configuration"
echo "  2. Add required environment variables to ${SERVICE_DIR}/.env"
echo "  3. Run: cd ${SERVICE_DIR} && sudo docker compose up -d"

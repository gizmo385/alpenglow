#!/usr/bin/env bash
set -euo pipefail

caddy_container_id=$(sudo docker ps | grep caddy | awk '{print $1;}')
sudo docker exec -w /etc/caddy $caddy_container_id caddy reload


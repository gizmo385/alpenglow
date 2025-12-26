#!/usr/bin/env bash

container_id=$(sudo docker ps | grep nextcloud-app | awk '{print $1}')

sudo docker exec -it "$container_id" php occ files:scan --all

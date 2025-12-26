#!/usr/bin/env bash
# This script is designed to be run via a cron and check the current health of our ZFS pools.

SCRIPT_DIR=$(dirname "$0")

. ${SCRIPT_DIR}/.env

function check_pool_state() {
	local POOL_STATE=$(zpool status $1 | sed '/.*[Ss]tate.*: */!d; s///; s/^[[:space:]]*//; s/[[:space:]]*$//;');
	echo "Pool state for $1 is $POOL_STATE"
	if [ "$POOL_STATE" == "ONLINE" ]
	then
		return 0
	else
		return 1
	fi
}

if check_pool_state "rpool"; then
	curl "${RPOOL_HEALTH_CHECK_ENDPOINT}?status=up&msg=healthy"
else
	curl "${RPOOL_HEALTH_CHECK_ENDPOINT}?status=down&msg=unhealthy"
fi

if check_pool_state "dpool"; then
	curl "${DPOOL_HEALTH_CHECK_ENDPOINT}?status=up&msg=healthy"
else
	curl "${DPOOL_HEALTH_CHECK_ENDPOINT}?status=down&msg=unhealthy"
fi

#!/usr/bin/env bash
# This script is designed to be run via a cron and check the current health of our ZFS pools.
#
# It does two things on every run:
#   1. Pushes per-pool health (up/down) to Uptime Kuma push monitors (endpoints from .env).
#   2. Writes /services/alpenglow_dashboard/data/zfs_status.json — a status feed consumed by the
#      Alpenglow Dashboard's Storage & ZFS tile (see PLAN.md work package B4 / integrations/zfs.py).
#
# Cron cadence: run every 5 minutes (the dashboard tolerates a slightly stale feed and only reads
# the file; the JSON's generated_at lets the UI show freshness). Example crontab entry:
#   */5 * * * * /services/zfs_status_checker/verify_status.sh >/dev/null 2>&1
#
# ZFS JSON note: `zpool status -j` / `zpool list -j` are only available on zfs >= 2.3. The host
# currently runs zfs 2.2.2, which lacks JSON output, so this script parses the text output of
# `zpool status <pool>` and the machine-parsable `zpool list -Hp <pool>`. If a future zfs upgrade
# adds `-j`, the text parsing below still works and can be swapped later.
#
# zfs_status.json schema (all sizes in bytes; timestamps are unix epoch seconds unless noted):
#   {
#     "generated_at": <int epoch>,          # when this file was written
#     "generated_at_iso": "<ISO-8601>",     # same instant, human-readable, local tz
#     "pools": {
#       "<poolname>": {
#         "name": "<poolname>",
#         "state": "ONLINE" | "DEGRADED" | "FAULTED" | "OFFLINE" | "UNAVAIL" | "REMOVED" | "UNKNOWN",
#         "healthy": true|false,            # true only when state == ONLINE
#         "size": <int bytes>,              # total pool size    (null if unknown)
#         "used": <int bytes>,              # allocated bytes    (null if unknown)
#         "free": <int bytes>,              # free bytes         (null if unknown)
#         "capacity_pct": <int 0-100>,      # % capacity used    (null if unknown)
#         "fragmentation_pct": <int>|null,  # % fragmentation
#         "used_human": "<e.g. 10.1T>",     # human-readable used (from `zpool list`)
#         "size_human": "<e.g. 10.9T>",     # human-readable size (from `zpool list`)
#         "last_scrub": <int epoch>|null,   # completion time of the last scrub
#         "last_scrub_iso": "<ISO-8601>"|null,
#         "scrub_repaired": "<e.g. 0B>"|null,
#         "scrub_errors": <int>|null,       # errors reported by the last scrub scan line
#         "read_errors": <int>,             # top-level vdev READ error count
#         "write_errors": <int>,            # top-level vdev WRITE error count
#         "checksum_errors": <int>,         # top-level vdev CKSUM error count
#         "data_errors": "<e.g. No known data errors>"  # the `errors:` line verbatim
#       },
#       ...
#     }
#   }
#
# Output file permissions: the file is written world-readable (0644) so the dashboard container can
# read it regardless of container UID. When possible it is also chgrp'd to `service-owners` (the
# repo's shared group). If the script cannot chgrp (not run as root/owner), world-read still suffices.

set -u

# zpool lives in /usr/sbin; make sure minimal cron PATHs still resolve it.
PATH="${PATH}:/usr/sbin:/sbin"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

POOLS=(rpool dpool)

DASHBOARD_DATA_DIR="/services/alpenglow_dashboard/data"
ZFS_STATUS_JSON="${DASHBOARD_DATA_DIR}/zfs_status.json"
DATA_GROUP="service-owners"

# ---------------------------------------------------------------------------
# Uptime Kuma health pushes (unchanged behavior)
# ---------------------------------------------------------------------------

if [ -f "${SCRIPT_DIR}/.env" ]; then
	# shellcheck disable=SC1091
	. "${SCRIPT_DIR}/.env"
else
	echo "WARNING: ${SCRIPT_DIR}/.env not found; skipping Uptime Kuma pushes" >&2
fi

function check_pool_state() {
	local POOL_STATE
	POOL_STATE=$(zpool status "$1" | sed '/.*[Ss]tate.*: */!d; s///; s/^[[:space:]]*//; s/[[:space:]]*$//;')
	echo "Pool state for $1 is $POOL_STATE"
	if [ "$POOL_STATE" == "ONLINE" ]; then
		return 0
	else
		return 1
	fi
}

if [ -n "${RPOOL_HEALTH_CHECK_ENDPOINT:-}" ]; then
	if check_pool_state "rpool"; then
		curl "${RPOOL_HEALTH_CHECK_ENDPOINT}?status=up&msg=healthy"
	else
		curl "${RPOOL_HEALTH_CHECK_ENDPOINT}?status=down&msg=unhealthy"
	fi
fi

if [ -n "${DPOOL_HEALTH_CHECK_ENDPOINT:-}" ]; then
	if check_pool_state "dpool"; then
		curl "${DPOOL_HEALTH_CHECK_ENDPOINT}?status=up&msg=healthy"
	else
		curl "${DPOOL_HEALTH_CHECK_ENDPOINT}?status=down&msg=unhealthy"
	fi
fi

# ---------------------------------------------------------------------------
# JSON status feed for the Alpenglow Dashboard
# ---------------------------------------------------------------------------

# json_str <string> — emit a JSON-escaped, double-quoted string (or null for empty input).
function json_str() {
	local s="$1"
	if [ -z "$s" ]; then
		printf 'null'
		return
	fi
	# Escape backslash, double-quote, and control chars minimally.
	s=${s//\\/\\\\}
	s=${s//\"/\\\"}
	s=${s//$'\t'/ }
	s=${s//$'\n'/ }
	printf '"%s"' "$s"
}

# json_num <value> — emit a bare number, or null if empty/non-numeric.
function json_num() {
	local n="$1"
	if [[ "$n" =~ ^-?[0-9]+$ ]]; then
		printf '%s' "$n"
	else
		printf 'null'
	fi
}

# scrub_epoch <status-text> — parse the scrub completion date from a `zpool status` scan line into
# unix epoch seconds. Returns empty string if not found / not parseable.
function scrub_epoch() {
	local status_text="$1"
	# Example: "scan: scrub repaired 0B in 07:48:14 with 0 errors on Sun Jun 14 08:12:15 2026"
	local scan_line date_part
	scan_line=$(printf '%s\n' "$status_text" | sed -n 's/^[[:space:]]*scan:[[:space:]]*//p' | head -1)
	# Grab everything after " on "
	date_part=$(printf '%s\n' "$scan_line" | sed -n 's/.* on \(.*\)$/\1/p')
	if [ -z "$date_part" ]; then
		printf ''
		return
	fi
	date -d "$date_part" +%s 2>/dev/null || printf ''
}

# Build the JSON body for a single pool. Emits `"<name>": { ... }` with no trailing comma.
function pool_json() {
	local pool="$1"

	local status_text
	status_text=$(zpool status "$pool" 2>/dev/null)

	# State (from the "state:" line).
	local state
	state=$(printf '%s\n' "$status_text" | sed -n 's/^[[:space:]]*state:[[:space:]]*//p' | head -1)
	[ -z "$state" ] && state="UNKNOWN"

	local healthy="false"
	[ "$state" == "ONLINE" ] && healthy="true"

	# Machine-parsable sizes: name size alloc free ckpoint expandsz frag cap dedup health altroot
	local size used free frag cap
	read -r _ size used free _ _ frag cap _ _ _ <<<"$(zpool list -Hp "$pool" 2>/dev/null)"

	# Human-readable used/size from the default `zpool list`.
	local used_human size_human
	read -r _ size_human used_human _ <<<"$(zpool list -H "$pool" 2>/dev/null)"

	# Top-level vdev error counts: the line whose first field equals the pool name.
	local read_err write_err cksum_err
	read -r read_err write_err cksum_err <<<"$(
		printf '%s\n' "$status_text" |
			awk -v p="$pool" '$1==p {print $3, $4, $5; exit}'
	)"
	[ -z "$read_err" ] && read_err=0
	[ -z "$write_err" ] && write_err=0
	[ -z "$cksum_err" ] && cksum_err=0

	# Scrub info.
	local scan_line scrub_repaired scrub_errors last_scrub last_scrub_iso
	scan_line=$(printf '%s\n' "$status_text" | sed -n 's/^[[:space:]]*scan:[[:space:]]*//p' | head -1)
	scrub_repaired=$(printf '%s\n' "$scan_line" | sed -n 's/.*repaired \([^ ]*\) in .*/\1/p')
	scrub_errors=$(printf '%s\n' "$scan_line" | sed -n 's/.*with \([0-9]*\) errors.*/\1/p')
	last_scrub=$(scrub_epoch "$status_text")
	if [ -n "$last_scrub" ]; then
		last_scrub_iso=$(date -d "@${last_scrub}" '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null)
	else
		last_scrub_iso=""
	fi

	# The verbatim "errors:" line.
	local data_errors
	data_errors=$(printf '%s\n' "$status_text" | sed -n 's/^errors:[[:space:]]*//p' | head -1)

	printf '    %s: {\n' "$(json_str "$pool")"
	printf '      "name": %s,\n' "$(json_str "$pool")"
	printf '      "state": %s,\n' "$(json_str "$state")"
	printf '      "healthy": %s,\n' "$healthy"
	printf '      "size": %s,\n' "$(json_num "$size")"
	printf '      "used": %s,\n' "$(json_num "$used")"
	printf '      "free": %s,\n' "$(json_num "$free")"
	printf '      "capacity_pct": %s,\n' "$(json_num "$cap")"
	printf '      "fragmentation_pct": %s,\n' "$(json_num "$frag")"
	printf '      "used_human": %s,\n' "$(json_str "$used_human")"
	printf '      "size_human": %s,\n' "$(json_str "$size_human")"
	printf '      "last_scrub": %s,\n' "$(json_num "$last_scrub")"
	printf '      "last_scrub_iso": %s,\n' "$(json_str "$last_scrub_iso")"
	printf '      "scrub_repaired": %s,\n' "$(json_str "$scrub_repaired")"
	printf '      "scrub_errors": %s,\n' "$(json_num "$scrub_errors")"
	printf '      "read_errors": %s,\n' "$(json_num "$read_err")"
	printf '      "write_errors": %s,\n' "$(json_num "$write_err")"
	printf '      "checksum_errors": %s,\n' "$(json_num "$cksum_err")"
	printf '      "data_errors": %s\n' "$(json_str "$data_errors")"
	printf '    }'
}

function write_zfs_status_json() {
	mkdir -p "$DASHBOARD_DATA_DIR" 2>/dev/null || {
		echo "WARNING: could not create ${DASHBOARD_DATA_DIR}; skipping zfs_status.json" >&2
		return 1
	}

	local now now_iso tmp
	now=$(date +%s)
	now_iso=$(date '+%Y-%m-%dT%H:%M:%S%z')
	tmp=$(mktemp "${DASHBOARD_DATA_DIR}/.zfs_status.json.XXXXXX" 2>/dev/null) || {
		echo "WARNING: could not create temp file in ${DASHBOARD_DATA_DIR}; skipping" >&2
		return 1
	}

	{
		printf '{\n'
		printf '  "generated_at": %s,\n' "$now"
		printf '  "generated_at_iso": %s,\n' "$(json_str "$now_iso")"
		printf '  "pools": {\n'
		local first=1
		for pool in "${POOLS[@]}"; do
			if [ "$first" -eq 0 ]; then
				printf ',\n'
			fi
			first=0
			pool_json "$pool"
		done
		printf '\n  }\n'
		printf '}\n'
	} >"$tmp"

	# World-readable so the dashboard container (arbitrary UID) can read it.
	chmod 0644 "$tmp" 2>/dev/null || true
	# Best-effort group ownership; harmless failure if not root/owner (world-read already covers us).
	chgrp "$DATA_GROUP" "$tmp" 2>/dev/null || true

	mv -f "$tmp" "$ZFS_STATUS_JSON"
	echo "Wrote ${ZFS_STATUS_JSON}"
}

write_zfs_status_json

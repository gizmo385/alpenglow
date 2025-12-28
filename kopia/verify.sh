#!/usr/bin/env bash
SCRIPT_DIR=$(dirname "$0")


# Source our environment variables
. ${SCRIPT_DIR}/.verify_env

# Configure the script
CONTAINER_NAME=Kopia
LOG_DIR="${SCRIPT_DIR}/verification_logs"
LOG_FILE="$LOG_DIR/`date +\%Y-\%m-\%d`-daily.log"
LOG_FILE_DAYS_TO_KEEP=30

FILE_PARALLELISM=10
PARALLELISM=10
VERIFY_FILES_PERCENT=1

SUCCESS_WEBHOOK="$BASE_UPTIME_KUMA_WEBHOOK?status=up&msg=Success"
FAILURE_WEBHOOK="$BASE_UPTIME_KUMA_WEBHOOK?status=down&msg=Failure"

if sudo docker exec -it $CONTAINER_NAME kopia snapshot verify \
	--verify-files-percent=$VERIFY_FILES_PERCENT \
	--file-parallelism=$FILE_PARALLELISM \
	--parallel=$PARALLELISM \
	2>&1 > $LOG_FILE \
	; then
    echo "Finished verifying files" | tee $LOG_FILE
    curl $SUCCESS_WEBHOOK
    find $LOG_DIR -maxdepth 1 -mtime +$LOG_FILE_DAYS_TO_KEEP -name "*-daily" -exec rm -rf '{}' ';'
else
    echo "Failed to verify backup snapshots, check logs at @${LOG_FILE}" | tee $LOG_FILE
    curl $FAILURE_WEBHOOK
fi

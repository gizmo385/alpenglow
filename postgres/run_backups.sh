#!/bin/bash

SCRIPT_DIR=$(dirname "$0")

. ${SCRIPT_DIR}/.backup_env

SUCCESS_WEBHOOK="$BACKUP_STATUS_UPTIME_MONITOR?status=up&msg=Success"
FAILURE_WEBHOOK="$BACKUP_STATUS_UPTIME_MONITOR?status=down&msg=Failure"

echo "Generating postgres database dump @ $BACKUP_FILE"
if sudo docker exec -t $CONTAINER_NAME pg_dumpall -U $BACKUP_USER > $BACKUP_FILE; then
	echo "Finished generating backup, scanning for old backups"
	curl $SUCCESS_WEBHOOK
	find $BACKUP_DIR -maxdepth 1 -mtime +$DAYS_TO_KEEP -name "*-daily" -exec rm -rf '{}' ';'
else
	echo "FAILED TO RUN THE BACKUP"
	curl $FAILURE_WEBHOOK
fi

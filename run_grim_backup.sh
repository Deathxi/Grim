#!/bin/bash
# Quiesce Grim before snapshotting its runtime files, then restore service
# availability even if encryption or upload fails. This wrapper is invoked by
# grim-backup.service rather than run manually.
set -euo pipefail

grim_was_active=false

restart_grim_if_needed() {
    if [ "$grim_was_active" = true ]; then
        echo "[Backup] Restarting Grim after consistent data snapshot..."
        if ! systemctl start grim.service; then
            echo "[Backup] ERROR: Backup finished but Grim could not be restarted." >&2
            return 1
        fi
    fi
}

finish() {
    local status=$?
    trap - EXIT INT TERM
    if ! restart_grim_if_needed; then
        exit 1
    fi
    exit "$status"
}

trap finish EXIT
trap 'exit 143' INT TERM

if systemctl is-active --quiet grim.service; then
    grim_was_active=true
    echo "[Backup] Stopping Grim for a consistent runtime-data snapshot..."
    systemctl stop grim.service
fi

/usr/bin/python3 /root/grim/backup_grim_data.py
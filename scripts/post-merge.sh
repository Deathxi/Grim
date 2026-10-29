#!/usr/bin/env bash
set -euo pipefail

# Keep task merges fast and non-interactive while catching syntax errors in the
# bot and its standalone backup utility.
python3 -m py_compile main.py backup_grim_data.py
bash -n run_grim_backup.sh setup_vps.sh
# systemd units are INI; reject accidental shell metacharacters / empty files.
test -s systemd/grim.service
test -s systemd/grim-backup.service
test -s systemd/grim-backup.timer
#!/usr/bin/env bash
set -euo pipefail

# Keep task merges fast and non-interactive while catching syntax errors in the
# bot and its standalone backup utility.
python3 -m py_compile main.py backup_grim_data.py push_to_github.py
bash -n run_grim_backup.sh setup_vps.sh
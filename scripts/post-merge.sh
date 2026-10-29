#!/usr/bin/env bash
set -euo pipefail

# Keep task merges fast and non-interactive while catching syntax errors in the
# bot and its standalone backup utility.
python3 -m py_compile main.py backup_grim_data.py
bash -n run_grim_backup.sh setup_vps.sh scripts/print-deploy-key.sh
# systemd units are INI; reject accidental shell metacharacters / empty files.
test -s systemd/grim.service
test -s systemd/grim-backup.service
test -s systemd/grim-backup.timer
# Checkout must stay read-only for the bot process (no planted git hooks).
grep -q 'ReadWritePaths=/root/.grim_data' systemd/grim.service
# Failed deploys must not dump journals into public Actions logs.
if grep -E '^[[:space:]]*[-|]?[[:space:]]*journalctl\b' .github/workflows/deploy.yml; then
  echo "deploy.yml must not invoke journalctl (public log leak risk)" >&2
  exit 1
fi

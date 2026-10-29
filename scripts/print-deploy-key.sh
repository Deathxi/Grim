#!/usr/bin/env bash
# Opt-in printer for the GitHub Actions deploy private key.
# Requires CONFIRM=print-key to avoid accidental terminal/session log exposure.
set -euo pipefail

if [ "${CONFIRM:-}" != "print-key" ]; then
    echo "Refusing to print. Re-run with: CONFIRM=print-key $0" >&2
    exit 1
fi

KEY_PATH="${1:-/root/.ssh/github_deploy}"
if [ ! -f "${KEY_PATH}" ]; then
    echo "Deploy key not found at ${KEY_PATH}" >&2
    exit 1
fi

echo "WARNING: this private key grants root SSH. Paste only into GitHub Actions secrets." >&2
cat "${KEY_PATH}"
echo

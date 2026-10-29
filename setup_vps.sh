#!/bin/bash
set -euo pipefail
umask 077

echo "======================================"
echo "  Grim VPS Setup"
echo "======================================"

# ── System packages ────────────────────────────────────
echo ""
echo "[1/7] Updating system and installing dependencies..."
apt-get update -q
apt-get install -y -q python3 python3-pip python3-venv git ffmpeg libopus-dev openssl

# ── Clone repo ─────────────────────────────────────────
echo ""
echo "[2/7] Cloning Grim from GitHub..."
if [ -d "/root/grim" ]; then
    echo "  /root/grim already exists — fetching latest..."
    cd /root/grim
    git fetch origin main
    GIT_CONFIG_COUNT=1 \
      GIT_CONFIG_KEY_0=core.hooksPath \
      GIT_CONFIG_VALUE_0=/dev/null \
      git reset --hard origin/main
else
    git clone https://github.com/Deathxi/Grim.git /root/grim
    cd /root/grim
fi

# Runtime data outside the git checkout (survives deploys).
install -d -m 700 /root/.grim_data

# ── Python dependencies ────────────────────────────────
echo ""
echo "[3/7] Installing Python packages..."
PIP_NO_INPUT=1 pip3 install -q -r /root/grim/requirements.txt

# ── Secrets / environment file ─────────────────────────
echo ""
echo "[4/7] Setting up environment secrets..."
echo "  Enter each secret when prompted (input is hidden). Press Enter to skip optional ones."
echo ""

read -r -s -p "  DISCORD_TOKEN: " DISCORD_TOKEN
echo ""
if [ -z "${DISCORD_TOKEN}" ]; then
    echo "  DISCORD_TOKEN is required; aborting."
    exit 1
fi
if [[ "${DISCORD_TOKEN}" == *$'\n'* ]] || [[ "${DISCORD_TOKEN}" == *$'\r'* ]]; then
    echo "  DISCORD_TOKEN must be a single line; aborting."
    exit 1
fi
read -r -s -p "  XAI_API_KEY: " XAI_API_KEY
echo ""
read -r -s -p "  X_BEARER_TOKEN: " X_BEARER_TOKEN
echo ""
echo "  GITHUB_PERSONAL_ACCESS_TOKEN is optional and must be READ-ONLY"
echo "  (Contents: Read on Deathxi/Grim for /grim_updates patch notes)."
echo "  Do NOT use a token that can push source, and never reuse the backup token."
read -r -s -p "  GITHUB_PERSONAL_ACCESS_TOKEN (optional): " GITHUB_PAT
echo ""
read -r -s -p "  OPENSEA_API_KEY (optional, press Enter to skip): " OPENSEA_KEY
echo ""

umask 077
: > /root/grim/.env
printf 'DISCORD_TOKEN=%s\n' "${DISCORD_TOKEN}" >> /root/grim/.env
printf 'XAI_API_KEY=%s\n' "${XAI_API_KEY}" >> /root/grim/.env
printf 'X_BEARER_TOKEN=%s\n' "${X_BEARER_TOKEN}" >> /root/grim/.env

if [ -n "${GITHUB_PAT}" ]; then
    printf 'GITHUB_PERSONAL_ACCESS_TOKEN=%s\n' "${GITHUB_PAT}" >> /root/grim/.env
fi

if [ -n "${OPENSEA_KEY}" ]; then
    printf 'OPENSEA_API_KEY=%s\n' "${OPENSEA_KEY}" >> /root/grim/.env
fi

chmod 600 /root/grim/.env
unset DISCORD_TOKEN XAI_API_KEY X_BEARER_TOKEN GITHUB_PAT OPENSEA_KEY
echo "  Secrets saved to /root/grim/.env (mode 0600)"

# ── Weekly encrypted backup configuration ───────────────
echo ""
echo "[5/7] Setting up optional weekly encrypted backups..."
echo "  Leave the repository blank to skip backup setup for now."
read -p "  Private backup repository (owner/repo): " BACKUP_REPO

if [ -n "$BACKUP_REPO" ]; then
    if [[ ! "$BACKUP_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "  Backup repository must use the owner/repository format; aborting."
        exit 1
    fi
    read -r -s -p "  Fine-grained backup token (this repo only): " BACKUP_TOKEN
    echo ""
    read -r -s -p "  Offline backup recovery passphrase: " BACKUP_PASSPHRASE
    echo ""
    if [ -z "$BACKUP_TOKEN" ] || [ -z "$BACKUP_PASSPHRASE" ]; then
        echo "  Backup repository, token, and passphrase are all required; aborting."
        exit 1
    fi

    install -d -m 700 /root/.config/grim-backup
    printf '%s' "$BACKUP_PASSPHRASE" > /root/.config/grim-backup/passphrase
    chmod 600 /root/.config/grim-backup/passphrase
    unset BACKUP_PASSPHRASE

    : > /etc/grim-backup.env
    printf 'GRIM_BACKUP_GITHUB_REPO=%s\n' "$BACKUP_REPO" >> /etc/grim-backup.env
    printf 'GRIM_BACKUP_GITHUB_TOKEN=%s\n' "$BACKUP_TOKEN" >> /etc/grim-backup.env
    printf 'GRIM_BACKUP_PASSPHRASE_FILE=%s\n' "/root/.config/grim-backup/passphrase" >> /etc/grim-backup.env
    printf 'GRIM_BACKUP_RETENTION=%s\n' "8" >> /etc/grim-backup.env
    chmod 600 /etc/grim-backup.env
    unset BACKUP_TOKEN

    install -m 644 /root/grim/systemd/grim-backup.service \
        /etc/systemd/system/grim-backup.service
    install -m 644 /root/grim/systemd/grim-backup.timer \
        /etc/systemd/system/grim-backup.timer
    systemctl daemon-reload
    systemctl enable --now grim-backup.timer
    echo "  Weekly backup timer enabled for Sundays at 03:00 UTC (+ up to 30 minutes)."
else
    echo "  Backup setup skipped. Re-run this section with a private repo before relying on backups."
fi

# ── Systemd service ────────────────────────────────────
echo ""
echo "[6/7] Creating systemd service..."

install -m 644 /root/grim/systemd/grim.service /etc/systemd/system/grim.service

systemctl daemon-reload
systemctl enable grim
systemctl start grim
echo "  Grim service created and started."

# ── GitHub Actions deploy key ──────────────────────────
echo ""
echo "[7/7] Generating SSH deploy key for GitHub Actions..."
install -d -m 700 /root/.ssh
if [ ! -f /root/.ssh/github_deploy ]; then
    ssh-keygen -t ed25519 -f /root/.ssh/github_deploy -N "" -q \
        -C "grim-github-actions-deploy"
    echo "  Created new deploy key at /root/.ssh/github_deploy"
else
    echo "  Reusing existing deploy key at /root/.ssh/github_deploy"
fi
chmod 600 /root/.ssh/github_deploy
chmod 644 /root/.ssh/github_deploy.pub
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

DEPLOY_PUB="$(cat /root/.ssh/github_deploy.pub)"
# Restrict forwarding/pty; leave interactive shell possible until a forced-command
# deploy user is approved (command= bricks deploys if the script path drifts).
DEPLOY_OPTS="no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty"
DEPLOY_LINE="${DEPLOY_OPTS} ${DEPLOY_PUB}"
if ! grep -qxF "${DEPLOY_LINE}" /root/.ssh/authorized_keys \
    && ! grep -qxF "${DEPLOY_PUB}" /root/.ssh/authorized_keys; then
    printf '%s\n' "${DEPLOY_LINE}" >> /root/.ssh/authorized_keys
elif grep -qxF "${DEPLOY_PUB}" /root/.ssh/authorized_keys \
    && ! grep -qxF "${DEPLOY_LINE}" /root/.ssh/authorized_keys; then
    # Upgrade a bare pubkey line from an older setup to restricted options.
    grep -vxF "${DEPLOY_PUB}" /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp
    printf '%s\n' "${DEPLOY_LINE}" >> /root/.ssh/authorized_keys.tmp
    mv /root/.ssh/authorized_keys.tmp /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

HOST_FINGERPRINT="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | awk '{print $2}' || true)"
PUBLIC_IP="$(curl -fsS https://ifconfig.me/ip 2>/dev/null || curl -fsS https://icanhazip.com 2>/dev/null || true)"

echo ""
echo "======================================"
echo "  Setup Complete!"
echo "======================================"
echo ""
echo "Grim is running. Check status with:"
echo "  systemctl status grim"
echo ""
echo "View live logs with:"
echo "  journalctl -u grim -f"
echo ""
echo "======================================"
echo "  IMPORTANT — GitHub Actions Setup"
echo "======================================"
echo ""
echo "Add these secrets to your GitHub repo"
echo "(github.com/Deathxi/Grim → Settings → Secrets → Actions):"
echo ""
echo "  Secret name:  VPS_HOST"
echo "  Secret value: ${PUBLIC_IP:-<droplet public IPv4>}"
echo ""
echo "  Secret name:  VPS_SSH_KEY"
echo "  Secret value: contents of /root/.ssh/github_deploy"
echo "  (run:  cat /root/.ssh/github_deploy )"
echo "  WARNING: that private key grants root SSH. Store it only in Actions"
echo "  secrets — never in chat, issues, or commits."
echo ""
echo "  Secret name:  VPS_SSH_FINGERPRINT  (recommended)"
echo "  Secret value: ${HOST_FINGERPRINT:-<run: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub>}"
echo ""
if [ "${CONFIRM:-}" = "print-key" ]; then
    echo "  CONFIRM=print-key set — printing private key once:"
    echo ""
    cat /root/.ssh/github_deploy
    echo ""
else
    echo "  Private key is NOT printed by default. Copy it yourself when ready:"
    echo "    cat /root/.ssh/github_deploy"
    echo "  Or re-print after setup with: CONFIRM=print-key /root/grim/scripts/print-deploy-key.sh"
    echo ""
fi

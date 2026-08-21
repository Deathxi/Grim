#!/bin/bash
set -e

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
    echo "  /root/grim already exists — pulling latest..."
    cd /root/grim && git pull origin main
else
    git clone https://github.com/Deathxi/Grim.git /root/grim
    cd /root/grim
fi

# ── Python dependencies ────────────────────────────────
echo ""
echo "[3/7] Installing Python packages..."
pip3 install -q -r /root/grim/requirements.txt

# ── Secrets / environment file ─────────────────────────
echo ""
echo "[4/7] Setting up environment secrets..."
echo "  Enter each secret when prompted. Press Enter to skip optional ones."
echo ""

read -p "  DISCORD_TOKEN: " DISCORD_TOKEN
read -p "  XAI_API_KEY: " XAI_API_KEY
read -p "  X_BEARER_TOKEN: " X_BEARER_TOKEN
read -p "  GITHUB_PERSONAL_ACCESS_TOKEN: " GITHUB_PAT
read -p "  OPENSEA_API_KEY (optional, press Enter to skip): " OPENSEA_KEY

cat > /root/grim/.env <<EOF
DISCORD_TOKEN=$DISCORD_TOKEN
XAI_API_KEY=$XAI_API_KEY
X_BEARER_TOKEN=$X_BEARER_TOKEN
GITHUB_PERSONAL_ACCESS_TOKEN=$GITHUB_PAT
EOF

if [ -n "$OPENSEA_KEY" ]; then
    echo "OPENSEA_API_KEY=$OPENSEA_KEY" >> /root/grim/.env
fi

chmod 600 /root/grim/.env
echo "  Secrets saved to /root/grim/.env"

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

    cat > /etc/grim-backup.env <<EOF
GRIM_BACKUP_GITHUB_REPO=$BACKUP_REPO
GRIM_BACKUP_GITHUB_TOKEN=$BACKUP_TOKEN
GRIM_BACKUP_PASSPHRASE_FILE=/root/.config/grim-backup/passphrase
GRIM_BACKUP_RETENTION=8
EOF
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

cat > /etc/systemd/system/grim.service <<EOF
[Unit]
Description=Grim Discord Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/grim
EnvironmentFile=/root/grim/.env
ExecStart=/usr/bin/python3 /root/grim/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable grim
systemctl start grim
echo "  Grim service created and started."

# ── GitHub Actions deploy key ──────────────────────────
echo ""
echo "[7/7] Generating SSH deploy key for GitHub Actions..."
ssh-keygen -t ed25519 -f /root/.ssh/github_deploy -N "" -q
cat /root/.ssh/github_deploy.pub >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

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
echo "Add these two secrets to your GitHub repo"
echo "(github.com/Deathxi/Grim → Settings → Secrets → Actions):"
echo ""
echo "  Secret name:  VPS_HOST"
echo "  Secret value: $(curl -s ifconfig.me)"
echo ""
echo "  Secret name:  VPS_SSH_KEY"
echo "  Secret value: (copy everything below, including the header/footer lines)"
echo ""
cat /root/.ssh/github_deploy
echo ""

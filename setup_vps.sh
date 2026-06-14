#!/bin/bash
set -e

echo "======================================"
echo "  Grim VPS Setup"
echo "======================================"

# ── System packages ────────────────────────────────────
echo ""
echo "[1/6] Updating system and installing dependencies..."
apt-get update -q
apt-get install -y -q python3 python3-pip python3-venv git ffmpeg libopus-dev

# ── Clone repo ─────────────────────────────────────────
echo ""
echo "[2/6] Cloning Grim from GitHub..."
if [ -d "/root/grim" ]; then
    echo "  /root/grim already exists — pulling latest..."
    cd /root/grim && git pull origin main
else
    git clone https://github.com/Deathxi/Grim.git /root/grim
    cd /root/grim
fi

# ── Python dependencies ────────────────────────────────
echo ""
echo "[3/6] Installing Python packages..."
pip3 install -q -r /root/grim/requirements.txt

# ── Secrets / environment file ─────────────────────────
echo ""
echo "[4/6] Setting up environment secrets..."
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

# ── Systemd service ────────────────────────────────────
echo ""
echo "[5/6] Creating systemd service..."

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
echo "[6/6] Generating SSH deploy key for GitHub Actions..."
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

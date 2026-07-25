#!/usr/bin/env bash
# One-shot setup for a fresh Debian/Ubuntu VM (sized for a GCP e2-micro).
# Usage: sudo bash setup.sh <git-clone-url>
set -euo pipefail

REPO_URL="${1:?usage: sudo bash setup.sh <git-clone-url>}"
APP_DIR=/opt/nba-draft-bot
ENV_FILE=/etc/draftbot.env

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

apt-get update -qq
apt-get install -y -qq git curl

# ponytail: 1G swap — installs can OOM a 1GB e2-micro without it
if ! swapon --show | grep -q .; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id draftbot &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin draftbot

[ -d "$APP_DIR" ] || git clone "$REPO_URL" "$APP_DIR"
chown -R draftbot:draftbot "$APP_DIR"

# uv brings its own Python if the OS one is older than 3.11
sudo -u draftbot bash -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u draftbot bash -c "cd '$APP_DIR' && ~/.local/bin/uv sync --frozen --no-dev"

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'EOF'
DISCORD_TOKEN=put-your-token-here
LLM_API_KEY=put-your-openrouter-key-here
SIM_MODEL=google/gemini-3.6-flash
#TEST_GUILD_ID=
EOF
    chmod 600 "$ENV_FILE"
fi

cp "$APP_DIR/deploy/draftbot.service" /etc/systemd/system/draftbot.service
systemctl daemon-reload
systemctl enable draftbot

echo
echo "Done. Now:"
echo "  1. sudo nano $ENV_FILE   # paste your real tokens"
echo "  2. sudo systemctl start draftbot"
echo "  3. journalctl -u draftbot -f   # watch it come up"

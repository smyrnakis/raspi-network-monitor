#!/bin/sh
set -eu

SERVICE_USER="raspi-monitor"
INSTALL_DIR="/opt/raspi-network-monitor"
CONFIG_DIR="/etc/raspi-network-monitor"
STATE_DIR="/var/lib/raspi-network-monitor"
SOURCE_DIR="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
CONFIG_SOURCE="${2:-$SOURCE_DIR/config.example.toml}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root." >&2
    exit 1
fi

for command in python3 ip ping systemctl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is missing: $command" >&2
        exit 1
    fi
done

if [ ! -f "$CONFIG_SOURCE" ]; then
    echo "Configuration source does not exist: $CONFIG_SOURCE" >&2
    exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "Python venv support is missing. Install python3-venv." >&2
    exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd \
        --system \
        --home-dir "$STATE_DIR" \
        --shell /usr/sbin/nologin \
        --user-group \
        "$SERVICE_USER"
fi

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR/settings"
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install "$SOURCE_DIR"

if [ ! -e "$CONFIG_DIR/config.toml" ]; then
    install -m 0640 -o root -g "$SERVICE_USER" \
        "$CONFIG_SOURCE" "$CONFIG_DIR/config.toml"
fi

install -m 0644 \
    "$SOURCE_DIR/deploy/systemd/raspi-network-monitor.service" \
    /etc/systemd/system/raspi-network-monitor.service
install -m 0644 \
    "$SOURCE_DIR/deploy/systemd/raspi-network-monitor-api.service" \
    /etc/systemd/system/raspi-network-monitor-api.service

systemctl daemon-reload

echo "Installation complete."
echo "Review $CONFIG_DIR/config.toml, then run:"
echo "  systemctl enable --now raspi-network-monitor.service"
echo "  systemctl enable --now raspi-network-monitor-api.service"

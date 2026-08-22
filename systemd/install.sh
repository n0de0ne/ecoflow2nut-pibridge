#!/usr/bin/env bash
# Bare-metal install helper for Raspberry Pi (Raspberry Pi OS Lite 64-bit).
#
# Installs system dependencies, creates the service user, sets up a virtualenv
# with the bridge package, drops NUT + bridge configuration in place and enables
# the systemd service.
#
# Run from a checkout of this repository:  sudo ./systemd/install.sh
set -euo pipefail

APP_DIR="/opt/ecoflow-nut-bridge"
CONF_DIR="/etc/ecoflow-nut"
NUT_CONF_DIR="/etc/nut"
SERVICE="ecoflow-nut-bridge.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
    exit 1
fi

echo "==> Installing system packages..."
apt-get update
apt-get install -y --no-install-recommends \
    bluez \
    nut-server \
    nut-client \
    python3 \
    python3-venv \
    python3-pip

echo "==> Creating service user 'ecoflow'..."
if ! id ecoflow >/dev/null 2>&1; then
    useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin ecoflow
fi
# Allow the service user to access BlueZ and write NUT state.
usermod -aG bluetooth ecoflow || true
usermod -aG nut ecoflow || true

echo "==> Installing application into ${APP_DIR}..."
mkdir -p "${APP_DIR}"
cp -r "${REPO_DIR}/src" "${REPO_DIR}/pyproject.toml" "${REPO_DIR}/README.md" "${APP_DIR}/"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
# Install with the optional server extras (aiohttp web UI + asyncpg Postgres
# logging). They are inert unless enabled in config.yaml, so this is safe even if
# you never turn the web UI on.
"${APP_DIR}/.venv/bin/pip" install "${APP_DIR}[server]"
# Home Assistant via MQTT discovery. Inert unless mqtt.enabled is set.
"${APP_DIR}/.venv/bin/pip" install "${APP_DIR}[mqtt]"
# The Eve outlet extra (aiohomekit), same deal: inert unless eve.enabled is set.
# Kept as its own best-effort step because it is the one dependency that has to
# build on some platforms, and a bridge that will not install because an
# *optional* outlet driver would not compile is a bad trade.
if ! "${APP_DIR}/.venv/bin/pip" install "${APP_DIR}[eve]"; then
    echo "==> NOTE: the optional [eve] extra (aiohomekit) did not install."
    echo "    Everything else is fine; only 'ecoflow-nut eve ...' needs it."
fi
chown -R ecoflow:nut "${APP_DIR}"
# Put the CLI on PATH. It lives in the venv, which is not on anyone's PATH, so
# without this every documented 'ecoflow-nut ...' command -- including the ones
# this script prints below -- fails with "command not found".
ln -sf "${APP_DIR}/.venv/bin/ecoflow-nut" /usr/local/bin/ecoflow-nut

echo "==> Installing NUT configuration into ${NUT_CONF_DIR}..."
mkdir -p "${NUT_CONF_DIR}"
# ups.conf and upsd.conf are structural (the UPS definition + LISTEN) and hold
# no secrets -- always install ours. The nut-server package ships empty defaults,
# so "keep existing" would leave upsd with no UPS and it fails to start. Back up
# any pre-existing file once to <file>.orig.
for f in ups.conf upsd.conf; do
    target="${NUT_CONF_DIR}/${f}"
    if [ -f "${target}" ] && [ ! -f "${target}.orig" ]; then
        cp "${target}" "${target}.orig"
    fi
    cp "${REPO_DIR}/nut/${f}" "${target}"
done
# upsd.users / upsmon.conf may hold user-set passwords -- only install ours if
# the existing file defines nothing (i.e. the empty package default).
if ! grep -q '^\[' "${NUT_CONF_DIR}/upsd.users" 2>/dev/null; then
    cp "${REPO_DIR}/nut/upsd.users" "${NUT_CONF_DIR}/upsd.users"
fi
if ! grep -q '^MONITOR' "${NUT_CONF_DIR}/upsmon.conf" 2>/dev/null; then
    cp "${REPO_DIR}/nut/upsmon.conf" "${NUT_CONF_DIR}/upsmon.conf"
fi
chown -R root:nut "${NUT_CONF_DIR}"
chmod 640 "${NUT_CONF_DIR}/upsd.users"
# NUT must run in "netserver" mode to serve upsd.
sed -i 's/^MODE=.*/MODE=netserver/' /etc/nut/nut.conf 2>/dev/null || \
    echo "MODE=netserver" > /etc/nut/nut.conf

echo "==> Installing bridge configuration into ${CONF_DIR}..."
mkdir -p "${CONF_DIR}"
if [ ! -f "${CONF_DIR}/config.yaml" ]; then
    cp "${REPO_DIR}/config/config.example.yaml" "${CONF_DIR}/config.yaml"
    echo "    *** edit ${CONF_DIR}/config.yaml with your device MAC/serial ***"
fi
chown -R ecoflow:nut "${CONF_DIR}"

echo "==> Installing systemd unit..."
cp "${REPO_DIR}/systemd/${SERVICE}" "/etc/systemd/system/${SERVICE}"

# Order NUT to start after the bridge, so the bridge's ExecStartPre 'seed' has
# written the dummy-ups state file before the driver tries to read it at boot.
# A drop-in for a unit that does not exist on this system is harmless.
for unit in nut-server.service nut-driver-enumerator.service; do
    dropin="/etc/systemd/system/${unit}.d"
    mkdir -p "${dropin}"
    cat > "${dropin}/ecoflow-bridge.conf" <<EOF
[Unit]
After=${SERVICE}
Wants=${SERVICE}
EOF
done

systemctl daemon-reload
systemctl enable nut-server.service 2>/dev/null || true
systemctl enable "${SERVICE}"

cat <<EOF

==> Done.

Next steps:
  1. Edit ${CONF_DIR}/config.yaml (MAC, serial, user_id; auto_shutdown if wanted).
  2. Edit ${NUT_CONF_DIR}/upsd.users to set real passwords.
  3. Start bridge: sudo systemctl start ${SERVICE}
  4. Start NUT:    sudo systemctl restart nut-server
  5. Verify:       upsc ecoflow@localhost:4141

Optional web UI (control dashboard):
  - In ${CONF_DIR}/config.yaml set 'web.enabled: true' and an 'auth_token'
    (or set ECOFLOW_WEB_TOKEN in a systemd drop-in), then restart the service.
  - Open http://<pi-ip>:8080
  - For Postgres history set 'postgres.enabled: true' and a 'dsn' (or
    ECOFLOW_PG_DSN). The aiohttp/asyncpg deps are already installed above.

Optional Home Assistant integration (MQTT discovery):
  - Set 'mqtt.enabled: true' and 'mqtt.host' to your broker, then restart.
  - Every sensor and switch appears in HA as one device. No YAML in HA.
  - Put the broker password in ECOFLOW_MQTT_PASSWORD via a systemd drop-in
    rather than in config.yaml, which is world-readable in /etc.

Optional Eve outlet (cut ONE downstream load, not the whole AC bank):
  - Reset the outlet and remove it from Apple Home first: a HomeKit accessory
    pairs to a single controller, and this makes the bridge that controller.
  - sudo ecoflow-nut --config /etc/ecoflow-nut/config.yaml eve discover  # -> device_id
    (sudo: the config and pairing files are not world-readable)
  - Put device_id + setup_code in the 'eve' section, then 'eve pair'.
  - Set 'eve.enabled: true' and restart; its tile appears on the dashboard.
  - eve.adapter defaults to hci1. A second BT dongle is strongly preferred --
    the EcoFlow link is persistent, and sharing one radio costs telemetry.
EOF

#!/usr/bin/env bash
# Upgrade an existing bare-metal install to the current checkout.
#
# The service does NOT run from your git tree: install.sh copies the source into
# /opt/ecoflow-nut-bridge and pip-installs it (non-editable) into a venv, and the
# unit runs that venv's entry point. So `git pull` on its own changes nothing --
# this script propagates the checkout through the copy, the venv and a restart.
#
# Deliberately narrower than install.sh: no apt, no NUT configuration, no service
# user, and it never touches your config.yaml. Use install.sh for a first install.
#
# Run from a checkout of this repository:  sudo ./systemd/update.sh
set -euo pipefail

APP_DIR="/opt/ecoflow-nut-bridge"
CONF_DIR="/etc/ecoflow-nut"
SERVICE="ecoflow-nut-bridge.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
    exit 1
fi

if [ ! -x "${APP_DIR}/.venv/bin/pip" ]; then
    echo "No existing install found at ${APP_DIR}." >&2
    echo "This script upgrades an install; run ./systemd/install.sh first." >&2
    exit 1
fi

# Report what is about to be installed. "I updated and nothing changed" is
# almost always a checkout sitting on the wrong branch or an unpulled commit,
# and that is invisible unless the script says so.
COMMIT="$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "==> Installing from ${REPO_DIR}"
echo "    branch ${BRANCH}, commit ${COMMIT}"
if [ -n "$(git -C "${REPO_DIR}" status --porcelain 2>/dev/null)" ]; then
    echo "    (working tree has uncommitted changes)"
fi

echo "==> Syncing source into ${APP_DIR}..."
# Remove first: cp -r merges, so files deleted upstream would otherwise linger
# in the copy and be packaged into the wheel.
rm -rf "${APP_DIR}/src"
cp -r "${REPO_DIR}/src" "${REPO_DIR}/pyproject.toml" "${REPO_DIR}/README.md" "${APP_DIR}/"
# Stale build metadata from the checkout would confuse the rebuild.
rm -rf "${APP_DIR}"/src/*.egg-info
# Record what went in, so `ecoflow-nut --version` can answer "which build is
# actually running" rather than leaving you to grep site-packages.
printf '%s\n' "${COMMIT}" > "${APP_DIR}/INSTALLED_COMMIT"

echo "==> Reinstalling into the virtualenv..."
# --force-reinstall because the version may not have changed between builds, and
# --no-deps to keep it quick; the follow-up install resolves any new dependency
# and is a no-op when nothing changed.
"${APP_DIR}/.venv/bin/pip" install --quiet --force-reinstall --no-deps "${APP_DIR}"
"${APP_DIR}/.venv/bin/pip" install --quiet "${APP_DIR}[server]"

# The web UI's assets ship as package-data. A bad glob installs cleanly and then
# serves a blank dashboard, which is the one failure mode worth catching here --
# CI installs in editable mode and can never see it.
echo "==> Verifying the web UI assets shipped..."
"${APP_DIR}/.venv/bin/python" - <<'PY'
import sys
from importlib.resources import files

missing = [
    name
    for name in ("index.html", "app.css", "app.js", "core.js", "chart.js", "views.js")
    if not files("ecoflow_nut").joinpath("static", name).is_file()
]
if missing:
    sys.exit("    MISSING from the installed package: " + ", ".join(missing))
print("    ok")
PY

# Prove the venv is actually running the code we just copied, not a stale build.
# A pip that quietly skipped the reinstall looks identical to a successful one
# until you go looking for a command that isn't there.
echo "==> Verifying the installed code matches the checkout..."
"${APP_DIR}/.venv/bin/python" - "${REPO_DIR}/src/ecoflow_nut/cli.py" <<'PY'
import hashlib
import sys
from pathlib import Path

import ecoflow_nut.cli as installed

source = Path(sys.argv[1]).read_bytes()
target = Path(installed.__file__).read_bytes()
if hashlib.sha256(source).digest() != hashlib.sha256(target).digest():
    sys.exit(
        f"    MISMATCH: {installed.__file__}\n"
        "    differs from the checkout -- the reinstall did not take. Check that\n"
        "    the checkout is on the branch you expect and that pip succeeded."
    )
print(f"    ok ({installed.__file__})")
PY

chown -R ecoflow:nut "${APP_DIR}"

echo "==> Refreshing the systemd unit..."
cp "${REPO_DIR}/systemd/${SERVICE}" "/etc/systemd/system/${SERVICE}"
systemctl daemon-reload

echo "==> Restarting ${SERVICE}..."
systemctl restart "${SERVICE}"
sleep 2
systemctl is-active --quiet "${SERVICE}" \
    && echo "    running" \
    || echo "    NOT running -- see: journalctl -u ${SERVICE} -n 50"

# Report what is actually switched on, using the freshly installed package's own
# loader. New features stay off until config.yaml enables them, and install.sh
# never rewrites an existing config -- so this is the usual "why is nothing
# different?" answer.
echo
echo "==> Active configuration (${CONF_DIR}/config.yaml):"
"${APP_DIR}/.venv/bin/python" - "${CONF_DIR}/config.yaml" <<'PY' || echo "    (could not read config)"
import sys

from ecoflow_nut.config import load_config

config = load_config(sys.argv[1])
store = (
    "postgres" if config.postgres.enabled
    else "sqlite" if config.sqlite.enabled
    else "none"
)
interval = (
    config.postgres.min_interval_seconds if config.postgres.enabled
    else config.sqlite.min_interval_seconds
)
print(f"    web UI          : {'on, port %d' % config.web.port if config.web.enabled else 'off (set web.enabled: true)'}")
print(f"    history store   : {store}" + ("" if store != "none" else " (set sqlite.enabled: true for charts)"))
if store != "none":
    print(f"    sample interval : {interval}s between stored rows")
PY

cat <<EOF

==> Done. Watch it come up with:
     journalctl -u ${SERVICE} -f
EOF

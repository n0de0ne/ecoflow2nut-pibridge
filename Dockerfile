# syntax=docker/dockerfile:1

# ---- builder: install the Python package into a venv ---------------------- #
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build deps for any wheels that need compiling (e.g. cryptography backends).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src
# Include the optional web UI + Postgres logging extras in the image.
RUN pip install '.[server]'

# The web UI's assets are package-data, so a bad glob would produce an image
# that serves a blank dashboard. CI installs in editable mode and can never
# catch that, so assert against the real installed wheel here.
RUN python -c "from importlib.resources import files; \
assert files('ecoflow_nut').joinpath('static', 'index.html').is_file(), \
'static assets missing from the installed wheel'"

# ---- runtime ------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Runtime deps: BlueZ for BLE, a D-Bus daemon for BlueZ to register on, and the
# NUT server + client. The container runs its own bluetoothd so it does not
# depend on the host having a BlueZ userspace.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
        nut-server \
        nut-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# NUT configuration. upsd drops privileges to the "nut" user (created by the
# nut-server package).
COPY nut/nut.conf nut/ups.conf nut/upsd.conf nut/upsd.users nut/upsmon.conf /etc/nut/
RUN chown -R nut:nut /etc/nut && chmod 640 /etc/nut/upsd.users

COPY config/config.example.yaml /app/config/config.example.yaml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app
# 4141 = NUT (upsd); 8080 = optional embedded web UI (when web.enabled).
EXPOSE 4141 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD upsc ecoflow@localhost:4141 >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

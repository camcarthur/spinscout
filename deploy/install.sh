#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# Spin Scout — Proxmox / Ubuntu / Debian one-shot installer.
#
# What this script does (idempotently — safe to re-run after `git pull`):
#   1. Installs system deps:  python3.11, node 20, postgres, nginx, certbot
#   2. Creates the `spinscout` system user and project directory
#   3. Sets up a Python virtualenv and installs backend requirements
#   4. Builds the React/Vite frontend to a static bundle served by nginx
#   5. Provisions the postgres database/user
#   6. Writes /etc/spinscout/spinscout.env  (preserves an existing one)
#   7. Installs `spinscout-api.service` (uvicorn) under systemd
#   8. Installs an nginx vhost for spinscout.net (frontend + /api/* reverse proxy)
#   9. Issues / renews a Let's Encrypt cert via certbot
#  10. Enables + starts everything
#
# Prerequisites:
#   * A fresh Ubuntu 22.04+ or Debian 12+ VM (Proxmox LXC containers also work
#     if they have systemd enabled — i.e. unprivileged with `nesting=1`).
#   * DNS A/AAAA records for spinscout.net (and optional www.spinscout.net)
#     already pointing at this VM's public IP.
#   * Ports 80 + 443 reachable from the public internet (required for the
#     Let's Encrypt HTTP-01 challenge).
#
# Run as root, from a checkout of the repo:
#     sudo bash deploy/install.sh
#
# Override with environment variables, e.g.:
#     sudo DOMAIN=spinscout.net STRAVA_CLIENT_ID=... bash deploy/install.sh
# ------------------------------------------------------------------------------

set -euo pipefail

# ---------- Configuration (override with env vars) ----------------------------

DOMAIN="${DOMAIN:-spinscout.net}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@${DOMAIN}}"     # Used by certbot for renewal notices
APP_USER="${APP_USER:-spinscout}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
APP_HOME="${APP_HOME:-/opt/spinscout}"            # Where the repo lives on the VM
WEB_ROOT="${WEB_ROOT:-/var/www/spinscout}"        # Where the built frontend is served from
ENV_DIR="${ENV_DIR:-/etc/spinscout}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/spinscout.env}"
API_PORT="${API_PORT:-8000}"
API_WORKERS="${API_WORKERS:-2}"

DB_NAME="${DB_NAME:-rideplanner}"
DB_USER="${DB_USER:-spinscout}"
# Auto-generate a DB password on first run; preserved on subsequent runs.
DB_PASSWORD_FILE="${ENV_DIR}/.db_password"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------- Helpers -----------------------------------------------------------

log()  { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m!! %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mxx %s\033[0m\n" "$*" >&2; exit 1; }

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    fail "This installer must be run as root. Try: sudo bash $0"
  fi
}

ensure_packages() {
  log "Installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y

  # Python 3.11 isn't always present in 22.04 base; deadsnakes covers that gap.
  if ! command -v python3.11 >/dev/null 2>&1; then
    apt-get install -y software-properties-common ca-certificates
    add-apt-repository -y ppa:deadsnakes/ppa || true
    apt-get update -y
  fi

  # Node 20 from NodeSource for the frontend build.
  if ! command -v node >/dev/null 2>&1 || [[ "$(node -v 2>/dev/null | cut -dv -f2 | cut -d. -f1)" -lt 20 ]]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  fi

  apt-get install -y \
    python3.11 python3.11-venv python3.11-dev \
    build-essential git curl ca-certificates \
    nodejs \
    postgresql postgresql-contrib libpq-dev \
    nginx \
    certbot python3-certbot-nginx \
    ufw
}

ensure_user() {
  if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    log "Creating service user ${APP_USER}"
    useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

stage_repo() {
  log "Staging repository at ${APP_HOME}"
  mkdir -p "${APP_HOME}"
  # Copy the working tree (excluding the existing venv & node_modules) into APP_HOME.
  rsync -a --delete \
    --exclude '.venv' \
    --exclude 'node_modules' \
    --exclude 'frontend/dist' \
    --exclude '.git/objects' \
    "${REPO_DIR}/" "${APP_HOME}/"
  chown -R "${APP_USER}:${APP_GROUP}" "${APP_HOME}"
}

setup_python_venv() {
  log "Setting up Python virtualenv"
  sudo -u "${APP_USER}" python3.11 -m venv "${APP_HOME}/.venv"
  sudo -u "${APP_USER}" "${APP_HOME}/.venv/bin/pip" install --upgrade pip wheel
  sudo -u "${APP_USER}" "${APP_HOME}/.venv/bin/pip" install -r "${APP_HOME}/backend/requirements.txt"
  # gunicorn is a more robust process manager than bare uvicorn for systemd.
  sudo -u "${APP_USER}" "${APP_HOME}/.venv/bin/pip" install "gunicorn>=22.0.0"
}

build_frontend() {
  log "Building frontend bundle"
  pushd "${APP_HOME}/frontend" >/dev/null
  # Point the built bundle at the public domain (served from the same origin via nginx).
  sudo -u "${APP_USER}" bash -lc "VITE_API_URL='https://${DOMAIN}' npm install --no-audit --no-fund && VITE_API_URL='https://${DOMAIN}' npm run build"
  popd >/dev/null

  log "Publishing frontend to ${WEB_ROOT}"
  mkdir -p "${WEB_ROOT}"
  rsync -a --delete "${APP_HOME}/frontend/dist/" "${WEB_ROOT}/"
  chown -R www-data:www-data "${WEB_ROOT}"
}

ensure_database() {
  log "Provisioning PostgreSQL database and role"
  systemctl enable --now postgresql

  mkdir -p "${ENV_DIR}"
  chmod 750 "${ENV_DIR}"
  if [[ ! -f "${DB_PASSWORD_FILE}" ]]; then
    umask 077
    openssl rand -base64 32 | tr -d '\n=+/' | cut -c1-40 > "${DB_PASSWORD_FILE}"
    chmod 600 "${DB_PASSWORD_FILE}"
  fi
  DB_PASSWORD="$(cat "${DB_PASSWORD_FILE}")"

  # Create role + db idempotently.
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';"
  sudo -u postgres psql -c "ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
    || sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"

  # Make sure the role can actually use it.
  sudo -u postgres psql -d "${DB_NAME}" -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" >/dev/null
  sudo -u postgres psql -d "${DB_NAME}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER};" >/dev/null
}

write_env_file() {
  log "Writing application env file at ${ENV_FILE}"
  mkdir -p "${ENV_DIR}"
  chmod 750 "${ENV_DIR}"

  DB_PASSWORD="$(cat "${DB_PASSWORD_FILE}")"
  local DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

  if [[ -f "${ENV_FILE}" ]]; then
    warn "${ENV_FILE} already exists — keeping your existing secrets, only refreshing host-derived values."
    # Refresh the values we own (URLs, DATABASE_URL); leave Strava/GraphHopper keys alone.
    sed -i \
      -e "s|^DATABASE_URL=.*|DATABASE_URL=${DATABASE_URL}|" \
      -e "s|^FRONTEND_URL=.*|FRONTEND_URL=https://${DOMAIN}|" \
      -e "s|^STRAVA_REDIRECT_URI=.*|STRAVA_REDIRECT_URI=https://${DOMAIN}/api/auth/strava/callback|" \
      "${ENV_FILE}"
  else
    cat > "${ENV_FILE}" <<EOF
# Spin Scout runtime environment (managed by deploy/install.sh).
# Strava + GraphHopper credentials must be filled in manually before first start.

DATABASE_URL=${DATABASE_URL}
REDIS_URL=redis://127.0.0.1:6379/0

ROUTING_PROVIDER=graphhopper
GRAPHHOPPER_API_KEY=
GRAPHHOPPER_BASE_URL=https://graphhopper.com/api/1

STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REDIRECT_URI=https://${DOMAIN}/api/auth/strava/callback
STRAVA_SCOPES=read,activity:read_all

FRONTEND_URL=https://${DOMAIN}

APP_SECRET=$(openssl rand -hex 32)
EOF
  fi

  chown root:"${APP_GROUP}" "${ENV_FILE}"
  chmod 640 "${ENV_FILE}"
}

write_systemd_unit() {
  log "Installing systemd unit /etc/systemd/system/spinscout-api.service"
  cat > /etc/systemd/system/spinscout-api.service <<EOF
[Unit]
Description=Spin Scout API (FastAPI/uvicorn)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_HOME}/backend
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_HOME}/.venv/bin/gunicorn app.main:app \\
    --workers ${API_WORKERS} \\
    --worker-class uvicorn.workers.UvicornWorker \\
    --bind 127.0.0.1:${API_PORT} \\
    --access-logfile - \\
    --error-logfile - \\
    --timeout 90
Restart=on-failure
RestartSec=3

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=${APP_HOME}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable spinscout-api.service
}

write_nginx_vhost() {
  log "Installing nginx vhost for ${DOMAIN}"
  local conf=/etc/nginx/sites-available/spinscout.conf
  cat > "${conf}" <<EOF
# Spin Scout — managed by deploy/install.sh
# Pre-TLS bootstrap: serves HTTP only so certbot --nginx can take over.
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};

    root ${WEB_ROOT};
    index index.html;

    # ACME challenge directory used by certbot.
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Frontend SPA.
    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # Backend API (FastAPI).
    location /api/ {
        proxy_pass http://127.0.0.1:${API_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }

    # Health endpoint convenience (also reachable via /api/health if you prefer).
    location = /health {
        proxy_pass http://127.0.0.1:${API_PORT}/health;
    }

    # Reasonable upload cap for GPX/TCX exports etc.
    client_max_body_size 4m;
}
EOF
  ln -sf "${conf}" /etc/nginx/sites-enabled/spinscout.conf
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl enable --now nginx
  systemctl reload nginx
}

issue_certificate() {
  log "Issuing/renewing TLS certificate via certbot for ${DOMAIN}"
  if certbot certificates 2>/dev/null | grep -q "Domains: ${DOMAIN}"; then
    warn "Certificate already exists — running renewal check only."
    certbot renew --quiet || true
  else
    # --redirect makes certbot rewrite the vhost to push HTTP→HTTPS automatically.
    certbot --nginx \
      --non-interactive --agree-tos --redirect \
      --email "${ADMIN_EMAIL}" \
      -d "${DOMAIN}" -d "www.${DOMAIN}" \
      || warn "certbot failed — the site will still serve over HTTP. Re-run after DNS propagates."
  fi

  # certbot installs a systemd timer for renewals; make sure it's enabled.
  systemctl enable --now certbot.timer 2>/dev/null || true
}

configure_firewall() {
  log "Configuring UFW firewall (SSH + HTTP/S only)"
  ufw allow OpenSSH || true
  ufw allow 'Nginx Full' || true
  yes | ufw enable >/dev/null 2>&1 || true
  ufw status verbose | sed 's/^/    /'
}

start_services() {
  log "Starting Spin Scout API"
  systemctl restart spinscout-api.service
  sleep 2
  systemctl --no-pager --full status spinscout-api.service | head -20 || true

  log "Reloading nginx"
  nginx -t
  systemctl reload nginx
}

print_next_steps() {
  cat <<EOF

─────────────────────────────────────────────────────────────
Spin Scout install complete.

Edit your secrets:
    sudo nano ${ENV_FILE}

The Strava + GraphHopper keys must be filled in before the
planner will fully work. After editing, restart the API:
    sudo systemctl restart spinscout-api

Status / logs:
    systemctl status spinscout-api
    journalctl -u spinscout-api -f
    journalctl -u nginx -f

App will be reachable at:  https://${DOMAIN}
DB:                        postgresql://${DB_USER}@127.0.0.1/${DB_NAME}
Web root:                  ${WEB_ROOT}
Repo on disk:              ${APP_HOME}

To deploy a new version after \`git pull\` in your working
directory, just re-run this script:
    sudo bash deploy/install.sh

It will rebuild the frontend, refresh the venv, and restart
the systemd service in place — your env file and TLS cert
are preserved.
─────────────────────────────────────────────────────────────
EOF
}

# ---------- Main --------------------------------------------------------------

main() {
  require_root
  ensure_packages
  ensure_user
  stage_repo
  setup_python_venv
  ensure_database
  write_env_file
  build_frontend
  write_systemd_unit
  write_nginx_vhost
  configure_firewall
  start_services
  issue_certificate
  start_services         # second pass picks up certbot's vhost rewrite
  print_next_steps
}

main "$@"

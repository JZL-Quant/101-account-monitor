#!/bin/bash
set -euo pipefail

# One-click HTTPS deployment for the account monitor.
# Defaults are set for the current production domain.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${PROJECT_DIR}/deploy/nginx/account-monitor.conf"

DOMAIN="${DOMAIN:-risk.jzlcapital.xyz}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@${DOMAIN}}"
APP_HOST="${MONITOR_NAV_HOST:-127.0.0.1}"
APP_PORT="${MONITOR_NAV_PORT:-7007}"
NGINX_CONF_PATH="${NGINX_CONF_PATH:-/etc/nginx/conf.d/account-monitor.conf}"
WEBROOT="${WEBROOT:-/var/www/certbot}"
START_SERVICE="${START_SERVICE:-1}"
CERT_FULLCHAIN_PATH=""
CERT_PRIVATE_KEY_PATH=""

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

error() {
    echo "Error: $*" >&2
    exit 1
}

need_file() {
    ${SUDO} test -f "$1" || error "Required file not found: $1"
}

install_packages() {
    echo "Installing nginx and certbot if needed..."
    if command -v apt-get >/dev/null 2>&1; then
        ${SUDO} apt-get update
        ${SUDO} apt-get install -y nginx certbot
    elif command -v dnf >/dev/null 2>&1; then
        ${SUDO} dnf install -y nginx certbot
    elif command -v yum >/dev/null 2>&1; then
        ${SUDO} yum install -y nginx certbot
    else
        error "Unsupported Linux package manager. Install nginx and certbot manually first."
    fi
}

ensure_nginx_running() {
    echo "Enabling and starting nginx..."
    ${SUDO} systemctl enable nginx
    ${SUDO} systemctl start nginx
}

reject_conflicting_domain_configs() {
    local target_real_path=""
    local file=""
    local real_path=""

    target_real_path="$(readlink -f "${NGINX_CONF_PATH}" 2>/dev/null || echo "${NGINX_CONF_PATH}")"
    echo "Checking for existing Nginx configs using ${DOMAIN}..."

    for file in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/*; do
        [ -f "${file}" ] || continue
        real_path="$(readlink -f "${file}" 2>/dev/null || echo "${file}")"
        [ "${real_path}" = "${target_real_path}" ] && continue

        if awk -v domain="${DOMAIN}" '
            $1 == "server_name" {
                for (i = 2; i <= NF; i++) {
                    gsub(";", "", $i)
                    if ($i == domain) found = 1
                }
            }
            END { exit(found ? 0 : 1) }
        ' "${file}"; then
            error "Domain ${DOMAIN} is already configured in ${file}. Use a different DOMAIN; existing Nginx configs were not changed."
        fi
    done
}

write_http_challenge_config() {
    echo "Writing temporary HTTP ACME challenge config for ${DOMAIN}..."
    ${SUDO} mkdir -p "${WEBROOT}/.well-known/acme-challenge"
    ${SUDO} mkdir -p "$(dirname "${NGINX_CONF_PATH}")"

    if [ -f "${NGINX_CONF_PATH}" ]; then
        local backup_path="${NGINX_CONF_PATH}.pre-cert.bak.$(date +'%Y%m%d_%H%M%S')"
        echo "Backing up existing Nginx config to ${backup_path}"
        ${SUDO} cp "${NGINX_CONF_PATH}" "${backup_path}"
    fi

    local tmp_conf
    tmp_conf="$(mktemp)"
    cat > "${tmp_conf}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root ${WEBROOT};
    }

    location / {
        proxy_pass http://${APP_HOST}:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto http;
    }
}
EOF
    ${SUDO} cp "${tmp_conf}" "${NGINX_CONF_PATH}"
    rm -f "${tmp_conf}"

    ${SUDO} nginx -t
    ${SUDO} systemctl reload nginx
}

obtain_certificate() {
    resolve_certificate_paths
    if [ -n "${CERT_FULLCHAIN_PATH}" ] && [ -n "${CERT_PRIVATE_KEY_PATH}" ]; then
        echo "Certificate already exists for ${DOMAIN}; skipping certbot issue."
        return 0
    fi

    echo "Requesting Let's Encrypt certificate for ${DOMAIN}..."
    ${SUDO} certbot certonly \
        --webroot \
        -w "${WEBROOT}" \
        -d "${DOMAIN}" \
        --agree-tos \
        --non-interactive \
        -m "${CERTBOT_EMAIL}"

    resolve_certificate_paths
}

resolve_certificate_paths() {
    local certificate_info=""

    certificate_info="$(${SUDO} certbot certificates --domain "${DOMAIN}" 2>/dev/null || true)"
    CERT_FULLCHAIN_PATH="$(printf '%s\n' "${certificate_info}" | awk '/Certificate Path:/ { print $3; exit }')"
    CERT_PRIVATE_KEY_PATH="$(printf '%s\n' "${certificate_info}" | awk '/Private Key Path:/ { print $4; exit }')"
}

write_final_https_config() {
    need_file "${TEMPLATE_PATH}"
    resolve_certificate_paths
    [ -n "${CERT_FULLCHAIN_PATH}" ] || error "Certbot did not report a certificate path for ${DOMAIN}."
    [ -n "${CERT_PRIVATE_KEY_PATH}" ] || error "Certbot did not report a private key path for ${DOMAIN}."
    need_file "${CERT_FULLCHAIN_PATH}"
    need_file "${CERT_PRIVATE_KEY_PATH}"

    echo "Writing final HTTPS reverse proxy config..."
    local tmp_conf
    tmp_conf="$(mktemp)"

    sed \
        -e "s/monitor.example.com/${DOMAIN}/g" \
        -e "s/server 127\\.0\\.0\\.1:7007;/server ${APP_HOST}:${APP_PORT};/g" \
        -e "s|/etc/letsencrypt/live/${DOMAIN}/fullchain.pem|${CERT_FULLCHAIN_PATH}|g" \
        -e "s|/etc/letsencrypt/live/${DOMAIN}/privkey.pem|${CERT_PRIVATE_KEY_PATH}|g" \
        "${TEMPLATE_PATH}" > "${tmp_conf}"

    ${SUDO} cp "${tmp_conf}" "${NGINX_CONF_PATH}"
    rm -f "${tmp_conf}"

    ${SUDO} nginx -t
    ${SUDO} systemctl reload nginx
}

start_monitor_service() {
    if [ "${START_SERVICE}" != "1" ]; then
        echo "Skipping service start because START_SERVICE=${START_SERVICE}"
        return 0
    fi

    echo "Starting account monitor service..."
    MONITOR_NAV_HOST="${APP_HOST}" MONITOR_NAV_PORT="${APP_PORT}" "${PROJECT_DIR}/start_account_monitor.sh"
}

print_summary() {
    echo
    echo "Deployment finished."
    echo "Domain: ${DOMAIN}"
    echo "HTTPS URL: https://${DOMAIN}/operations"
    echo "FastAPI upstream: ${APP_HOST}:${APP_PORT}"
    echo
    echo "Make sure the cloud security group/firewall exposes only 80 and 443 publicly."
    echo "Port ${APP_PORT} should not be open to the public internet."
}

echo "Project: ${PROJECT_DIR}"
echo "Domain: ${DOMAIN}"
echo "Certbot email: ${CERTBOT_EMAIL}"
echo "Nginx config: ${NGINX_CONF_PATH}"
echo "Upstream: ${APP_HOST}:${APP_PORT}"

install_packages
ensure_nginx_running
reject_conflicting_domain_configs
write_http_challenge_config
obtain_certificate
write_final_https_config
start_monitor_service
print_summary

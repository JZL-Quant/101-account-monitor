#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${PROJECT_DIR}/deploy/nginx/account-monitor.conf"

APP_HOST="${MONITOR_NAV_HOST:-127.0.0.1}"
APP_PORT="${MONITOR_NAV_PORT:-7007}"
NGINX_CONF_PATH="${NGINX_CONF_PATH:-/etc/nginx/conf.d/account-monitor.conf}"
DOMAIN="${DOMAIN:-}"
START_SERVICE="${START_SERVICE:-1}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

error() {
    echo "Error: $*" >&2
    exit 1
}

ensure_nginx_available() {
    command -v nginx >/dev/null 2>&1 || error \
        "Nginx is not installed. Run deploy/one_click_https_deploy.sh for the first deployment."
    command -v systemctl >/dev/null 2>&1 || error "systemctl is required to manage Nginx."
}

first_server_name_from_file() {
    local file="$1"
    [ -f "${file}" ] || return 0
    awk '
        $1 == "server_name" {
            for (i = 2; i <= NF; i++) {
                gsub(";", "", $i)
                if ($i != "_" && $i != "") {
                    print $i
                    exit
                }
            }
        }
    ' "${file}"
}

detect_existing_domain() {
    local detected=""

    detected="$(first_server_name_from_file "${NGINX_CONF_PATH}" || true)"
    if [ -n "${detected}" ]; then
        echo "${detected}"
        return 0
    fi

    for file in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/*; do
        [ -f "${file}" ] || continue
        detected="$(first_server_name_from_file "${file}" || true)"
        if [ -n "${detected}" ]; then
            echo "${detected}"
            return 0
        fi
    done
}

install_nginx_config() {
    [ -f "${TEMPLATE_PATH}" ] || error "Nginx template not found: ${TEMPLATE_PATH}"

    if [ -z "${DOMAIN}" ]; then
        DOMAIN="$(detect_existing_domain || true)"
    fi
    [ -n "${DOMAIN}" ] || error "No domain detected. Re-run with DOMAIN=your-domain.com"
    ${SUDO} test -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" || error \
        "TLS certificate not found for ${DOMAIN}. Run deploy/one_click_https_deploy.sh first."
    ${SUDO} test -f "/etc/letsencrypt/live/${DOMAIN}/privkey.pem" || error \
        "TLS private key not found for ${DOMAIN}. Run deploy/one_click_https_deploy.sh first."

    local tmp_conf
    tmp_conf="$(mktemp)"

    sed \
        -e "s/monitor.example.com/${DOMAIN}/g" \
        -e "s/server 127\\.0\\.0\\.1:7007;/server ${APP_HOST}:${APP_PORT};/g" \
        "${TEMPLATE_PATH}" > "${tmp_conf}"

    if [ -f "${NGINX_CONF_PATH}" ]; then
        local backup_path="${NGINX_CONF_PATH}.bak.$(date +'%Y%m%d_%H%M%S')"
        echo "Backing up existing Nginx config to ${backup_path}"
        ${SUDO} cp "${NGINX_CONF_PATH}" "${backup_path}"
    fi

    echo "Installing Nginx config: ${NGINX_CONF_PATH}"
    ${SUDO} mkdir -p "$(dirname "${NGINX_CONF_PATH}")"
    ${SUDO} cp "${tmp_conf}" "${NGINX_CONF_PATH}"
    rm -f "${tmp_conf}"

    echo "Testing Nginx config..."
    ${SUDO} nginx -t

    if [ "${RELOAD_NGINX}" = "1" ]; then
        echo "Enabling, starting, and reloading Nginx..."
        ${SUDO} systemctl enable --now nginx
        ${SUDO} systemctl reload nginx
    else
        echo "Skipping Nginx reload because RELOAD_NGINX=${RELOAD_NGINX}"
    fi
}

start_service() {
    if [ "${START_SERVICE}" != "1" ]; then
        echo "Skipping service start because START_SERVICE=${START_SERVICE}"
        return 0
    fi

    echo "Starting account monitor service on ${APP_HOST}:${APP_PORT}..."
    MONITOR_NAV_HOST="${APP_HOST}" MONITOR_NAV_PORT="${APP_PORT}" "${PROJECT_DIR}/start_account_monitor.sh"
}

echo "Project: ${PROJECT_DIR}"
echo "Nginx target: ${NGINX_CONF_PATH}"
echo "App upstream: ${APP_HOST}:${APP_PORT}"

ensure_nginx_available
install_nginx_config
start_service

echo "Done."
echo "Domain: ${DOMAIN}"
echo "HTTPS URL: https://${DOMAIN}/operations"

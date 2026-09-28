#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${WL_REPO_URL:-https://github.com/mojtaba-glx/Hiddify-WhiteLabel.git}"
INSTALL_DIR="${WL_INSTALL_DIR:-/opt/hiddify-whitelabel}"
SERVICE_USER="${WL_SERVICE_USER:-whitelabel}"
SERVICE_HOME="${WL_SERVICE_HOME:-/var/lib/hiddify-whitelabel}"
BRANCH="${WL_INSTALL_BRANCH:-main}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: run with sudo/root." >&2
    exit 1
fi

if [[ "$INSTALL_DIR" =~ [[:space:]] ]]; then
    echo "ERROR: installation path must not contain whitespace." >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "ERROR: the one-line installer currently supports Ubuntu/Debian (apt)." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends     ca-certificates curl git python3 python3-venv python3-pip

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$SERVICE_HOME"         --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$INSTALL_DIR")"

if [[ -d "$INSTALL_DIR/.git" ]]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    echo "Existing installation detected; invoking updater."
    chmod +x "$INSTALL_DIR/install.sh"
    "$INSTALL_DIR/install.sh" update </dev/tty
    exit $?
fi

if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" ]]; then
    if [[ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
        echo "ERROR: $INSTALL_DIR exists and is not an empty Git checkout." >&2
        exit 1
    fi
fi

rm -rf "$INSTALL_DIR"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
runuser -u "$SERVICE_USER" -- git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/bootstrap.sh" 2>/dev/null || true

echo
echo "Repository installed at: $INSTALL_DIR"
echo "Starting interactive application setup..."
"$INSTALL_DIR/install.sh" install </dev/tty

echo
echo "Installation complete."
echo "Open the manager anytime with:"
echo "  sudo whitelabel"

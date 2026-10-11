#!/usr/bin/env bash
# Local-dev manager for Hiddify-WhiteLabel (MasterBot + TenantRuntime).
# Usage: ./scripts/dev.sh {start|stop|restart|status|logs [master|runtime]}
# Safe to run from any directory: always switches to the project root.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/venv/bin/python" ]; then
    PY="$ROOT/venv/bin/python"
else
    PY="python3"
fi

SHARD_COUNT="${RUNTIME_SHARD_COUNT:-1}"
SHARD_INDEX="${RUNTIME_SHARD_INDEX:-0}"

is_running() { # $1 = pattern
    pgrep -f "$1" >/dev/null 2>&1
}

do_stop() {
    echo "Stopping bots..."
    pkill -f "TenantRuntime" 2>/dev/null || true
    pkill -f "[M]asterBot" 2>/dev/null || true
    sleep 2
    if is_running "TenantRuntime" || pgrep -f "python3 -m MasterBot" >/dev/null 2>&1 || pgrep -f ".venv/bin/python -m MasterBot" >/dev/null 2>&1; then
        echo "Forcing remaining processes..."
        pkill -9 -f "TenantRuntime" 2>/dev/null || true
        pkill -9 -f "MasterBot" 2>/dev/null || true
        sleep 1
    fi
    echo "Stopped."
}

do_start() {
    if [ ! -f "$ROOT/.env" ]; then
        echo "ERROR: .env not found. Copy .env.example to .env and fill tokens first." >&2
        exit 1
    fi
    echo "Starting MasterBot..."
    # shellcheck disable=SC2086
    nohup $PY -m MasterBot >>"$LOG_DIR/masterbot.log" 2>&1 &
    echo "Starting TenantRuntime (shards: count=$SHARD_COUNT index=$SHARD_INDEX)..."
    # shellcheck disable=SC2086
    RUNTIME_SHARD_COUNT="$SHARD_COUNT" RUNTIME_SHARD_INDEX="$SHARD_INDEX" \
        nohup $PY -m TenantRuntime >>"$LOG_DIR/tenant-runtime.log" 2>&1 &
    sleep 3
    do_status
}

do_status() {
    echo "--- MasterBot ---"
    if pgrep -af "MasterBot" | grep -v grep >/dev/null 2>&1; then
        pgrep -af "MasterBot" | grep -v grep
    else
        echo "stopped"
    fi
    echo "--- TenantRuntime ---"
    if is_running "TenantRuntime"; then
        pgrep -af "TenantRuntime" | grep -v grep
    else
        echo "stopped"
    fi
}

do_logs() {
    case "${1:-}" in
        master) tail -50 "$LOG_DIR/masterbot.log" 2>/dev/null || echo "no master log yet" ;;
        runtime) tail -50 "$LOG_DIR/tenant-runtime.log" 2>/dev/null || echo "no runtime log yet" ;;
        *) tail -30 "$LOG_DIR/masterbot.log" "$LOG_DIR/tenant-runtime.log" 2>/dev/null || echo "no logs yet" ;;
    esac
}

case "${1:-restart}" in
    start) do_start ;;
    stop) do_stop ;;
    restart) do_stop; do_start ;;
    status) do_status ;;
    logs) do_logs "${2:-}" ;;
    *) echo "Usage: $0 {start|stop|restart|status|logs [master|runtime]}"; exit 1 ;;
esac

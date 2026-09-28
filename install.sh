#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
SYSTEMD_DIR="/etc/systemd/system"
MASTER_UNIT="hiddify-whitelabel-master.service"
RUNTIME_TEMPLATE="hiddify-whitelabel-runtime@.service"
DRY_RUN=0
ACTION=""

for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        install|start|stop|restart|status|health|backup|restore|uninstall) ACTION="$argument" ;;
        *) echo "Unknown argument: $argument" >&2; exit 2 ;;
    esac
done

service_user() {
    stat -c '%U' "$ROOT_DIR"
}

require_root() {
    if [[ "$DRY_RUN" -eq 0 && "${EUID:-$(id -u)}" -ne 0 ]]; then
        echo "ERROR: this action must be run as root." >&2
        exit 1
    fi
}

shard_count() {
    local raw
    if [[ ! -f "$ENV_FILE" ]]; then
        echo 4
        return
    fi
    raw="$(sed -n 's/^RUNTIME_SHARD_COUNT=//p' "$ENV_FILE" | tail -n 1)"
    [[ -n "$raw" ]] || raw=4
    if [[ ! "$raw" =~ ^[1-9][0-9]*$ ]] || ((10#$raw > 64)); then
        echo "RUNTIME_SHARD_COUNT must be between 1 and 64" >&2
        return 1
    fi
    echo "$raw"
}

runtime_units() {
    runtime_units_for_count "$(shard_count)"
}

runtime_units_for_count() {
    local count="$1" index
    for ((index=0; index<count; index++)); do
        printf '%s\n' "hiddify-whitelabel-runtime@${index}.service"
    done
}

run_systemctl() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'DRY-RUN: systemctl'
        printf ' %q' "$@"
        printf '\n'
        return
    fi
    systemctl "$@"
}

as_service_user() {
    local user
    user="$(service_user)"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'DRY-RUN: as %s' "$user"
        printf ' %q' "$@"
        printf '\n'
        return
    fi
    if [[ "$(id -un)" == "$user" ]]; then
        "$@"
    elif [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        runuser -u "$user" -- "$@"
    else
        echo "ERROR: run this action as $user or root." >&2
        return 1
    fi
}

ensure_environment() {
    if [[ -f "$ENV_FILE" ]]; then
        chmod 600 "$ENV_FILE"
        chown "$(service_user)":"$(id -gn "$(service_user)")" "$ENV_FILE"
        return
    fi
    local master_token admin_id encryption_key temporary
    read -r -s -p "Master bot token (hidden): " master_token
    echo
    read -r -p "Master admin numeric ID: " admin_id
    if [[ "$master_token" != *:* || ! "$admin_id" =~ ^[1-9][0-9]*$ ]]; then
        unset master_token admin_id
        echo "ERROR: invalid token or admin ID." >&2
        return 1
    fi
    encryption_key="$("$ROOT_DIR/.venv/bin/python" - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
)"
    temporary="$ROOT_DIR/.env.tmp.$$"
    umask 077
    {
        printf 'MASTER_BOT_TOKEN=%s\n' "$master_token"
        printf 'MASTER_ADMIN_ID=%s\n' "$admin_id"
        printf 'TOKEN_ENCRYPTION_KEY=%s\n' "$encryption_key"
        cat <<'EOF'
DATABASE_PATH=data/whitelabel.db
DISPLAY_TIMEZONE=Asia/Tehran
LICENSE_JOB_INTERVAL_SECONDS=60
NOTIFICATION_LEASE_SECONDS=120
MAX_NOTIFICATION_RETRIES=5
NOTIFICATION_RETRY_BASE_SECONDS=30
RUNTIME_CACHE_TTL_SECONDS=15
RUNTIME_SHARD_COUNT=4
RUNTIME_SHARD_INDEX=0
RUNTIME_RECONCILE_SECONDS=15
RUNTIME_START_CONCURRENCY=8
RUNTIME_POLL_TIMEOUT_SECONDS=20
EOF
    } > "$temporary"
    chmod 600 "$temporary"
    chown "$(service_user)":"$(id -gn "$(service_user)")" "$temporary"
    mv "$temporary" "$ENV_FILE"
    unset master_token admin_id encryption_key
    echo "OK: secure .env created."
}

install_units() {
    local user render_dir rollback_dir unit old runtime_unit old_count new_count max_count
    user="$(service_user)"
    render_dir="$ROOT_DIR/runtime/systemd"
    rollback_dir="$ROOT_DIR/runtime/install-rollback/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    install -d -m 700 -o "$user" -g "$(id -gn "$user")" "$render_dir" "$rollback_dir"
    printf '%s\n' "$rollback_dir" > "$ROOT_DIR/runtime/last-unit-rollback"
    chmod 600 "$ROOT_DIR/runtime/last-unit-rollback"
    new_count="$(shard_count)"
    old_count="$new_count"
    if [[ -f "$ROOT_DIR/runtime/installed-shard-count" ]]; then
        old_count="$(cat "$ROOT_DIR/runtime/installed-shard-count")"
        [[ "$old_count" =~ ^[1-9][0-9]*$ ]] || old_count="$new_count"
    fi
    printf '%s\n' "$old_count" > "$rollback_dir/old-shard-count"
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/render_systemd.py" \
        --output-dir "$render_dir" --project-root "$ROOT_DIR" --service-user "$user"
    for unit in "$MASTER_UNIT" "$RUNTIME_TEMPLATE"; do
        old="$SYSTEMD_DIR/$unit"
        if [[ "$unit" == "$MASTER_UNIT" ]]; then
            systemctl is-active --quiet "$unit" && : > "$rollback_dir/$unit.active" || true
            systemctl is-enabled --quiet "$unit" && : > "$rollback_dir/$unit.enabled" || true
        fi
        if [[ -f "$old" ]]; then
            cp -a "$old" "$rollback_dir/$unit.previous"
        else
            : > "$rollback_dir/$unit.absent"
        fi
        install -m 644 "$render_dir/$unit" "$old"
    done
    max_count="$new_count"; (( old_count > max_count )) && max_count="$old_count"
    while IFS= read -r runtime_unit; do
        systemctl is-active --quiet "$runtime_unit" && : > "$rollback_dir/$runtime_unit.active" || true
        systemctl is-enabled --quiet "$runtime_unit" && : > "$rollback_dir/$runtime_unit.enabled" || true
    done < <(runtime_units_for_count "$max_count")
}

rollback_units() {
    local rollback_dir unit target runtime_unit old_count new_count max_count
    [[ -f "$ROOT_DIR/runtime/last-unit-rollback" ]] || return 0
    rollback_dir="$(cat "$ROOT_DIR/runtime/last-unit-rollback")"
    systemctl stop "$MASTER_UNIT" >/dev/null 2>&1 || true
    old_count="$(cat "$rollback_dir/old-shard-count" 2>/dev/null || shard_count)"
    new_count="$(shard_count)"
    max_count="$new_count"; (( old_count > max_count )) && max_count="$old_count"
    while IFS= read -r runtime_unit; do
        systemctl stop "$runtime_unit" >/dev/null 2>&1 || true
    done < <(runtime_units_for_count "$max_count")
    for unit in "$MASTER_UNIT" "$RUNTIME_TEMPLATE"; do
        target="$SYSTEMD_DIR/$unit"
        if [[ -f "$rollback_dir/$unit.previous" ]]; then
            cp -a "$rollback_dir/$unit.previous" "$target"
        elif [[ -f "$rollback_dir/$unit.absent" ]]; then
            systemctl disable "$unit" >/dev/null 2>&1 || true
            rm -f "$target"
        fi
    done
    systemctl daemon-reload || true
    if [[ -f "$rollback_dir/$MASTER_UNIT.enabled" ]]; then
        systemctl enable "$MASTER_UNIT" >/dev/null 2>&1 || true
    else
        systemctl disable "$MASTER_UNIT" >/dev/null 2>&1 || true
    fi
    [[ -f "$rollback_dir/$MASTER_UNIT.active" ]] && systemctl start "$MASTER_UNIT" >/dev/null 2>&1 || true
    while IFS= read -r runtime_unit; do
        if [[ -f "$rollback_dir/$runtime_unit.enabled" ]]; then
            systemctl enable "$runtime_unit" >/dev/null 2>&1 || true
        else
            systemctl disable "$runtime_unit" >/dev/null 2>&1 || true
        fi
        [[ -f "$rollback_dir/$runtime_unit.active" ]] && systemctl start "$runtime_unit" >/dev/null 2>&1 || true
    done < <(runtime_units_for_count "$max_count")
    echo "ROLLBACK: previous systemd unit files restored." >&2
}

start_services() {
    local unit
    run_systemctl start "$MASTER_UNIT"
    while IFS= read -r unit; do run_systemctl start "$unit"; done < <(runtime_units)
}

stop_services() {
    local unit
    while IFS= read -r unit; do run_systemctl stop "$unit"; done < <(runtime_units)
    run_systemctl stop "$MASTER_UNIT"
}

enable_services() {
    local unit
    run_systemctl enable "$MASTER_UNIT"
    while IFS= read -r unit; do run_systemctl enable "$unit"; done < <(runtime_units)
}

disable_obsolete_shards() {
    local old_count new_count index unit
    new_count="$(shard_count)"
    old_count="$new_count"
    if [[ -f "$ROOT_DIR/runtime/installed-shard-count" ]]; then
        old_count="$(cat "$ROOT_DIR/runtime/installed-shard-count")"
        [[ "$old_count" =~ ^[1-9][0-9]*$ ]] || old_count="$new_count"
    fi
    if (( old_count > new_count )); then
        for ((index=new_count; index<old_count; index++)); do
            unit="hiddify-whitelabel-runtime@${index}.service"
            run_systemctl stop "$unit" || true
            run_systemctl disable "$unit" || true
        done
    fi
}

disable_services() {
    local unit
    while IFS= read -r unit; do run_systemctl disable "$unit" || true; done < <(runtime_units)
    run_systemctl disable "$MASTER_UNIT" || true
}

status_services() {
    local unit
    run_systemctl --no-pager --full status "$MASTER_UNIT" || true
    while IFS= read -r unit; do run_systemctl --no-pager --full status "$unit" || true; done < <(runtime_units)
}

health_services() {
    local unit failed=0
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/healthcheck.py" --env-file "$ENV_FILE" || failed=1
    if [[ "$DRY_RUN" -eq 0 ]]; then
        systemctl is-active --quiet "$MASTER_UNIT" || { echo "ERROR: $MASTER_UNIT is not active."; failed=1; }
        while IFS= read -r unit; do
            systemctl is-active --quiet "$unit" || { echo "ERROR: $unit is not active."; failed=1; }
        done < <(runtime_units)
    fi
    return "$failed"
}

install_all() {
    require_root
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "DRY-RUN: create venv, secure .env/data directories, migrate DB, install units and start services."
        run_systemctl daemon-reload
        enable_services
        start_services
        return
    fi
    local user group
    user="$(service_user)"; group="$(id -gn "$user")"
    install -d -m 700 -o "$user" -g "$group" "$ROOT_DIR/data" "$ROOT_DIR/runtime" "$ROOT_DIR/backups" "$ROOT_DIR/logs"
    if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
        as_service_user python3 -m venv "$ROOT_DIR/.venv"
    fi
    as_service_user "$ROOT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT_DIR/requirements.txt"
    ensure_environment
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/migrate.py" --env-file "$ENV_FILE"
    trap rollback_units ERR
    install_units
    systemctl daemon-reload
    disable_obsolete_shards
    enable_services
    start_services
    health_services
    printf '%s\n' "$(shard_count)" > "$ROOT_DIR/runtime/installed-shard-count"
    chmod 600 "$ROOT_DIR/runtime/installed-shard-count"
    trap - ERR
    echo "OK: installation completed."
}

backup_action() {
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/backup.py" --env-file "$ENV_FILE"
}

restore_action() {
    local backup_file
    read -r -p "Encrypted backup file: " backup_file
    stop_services
    if as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/restore.py" "$backup_file" --env-file "$ENV_FILE" --yes; then
        start_services
        health_services
    else
        echo "ERROR: restore failed; starting existing installation." >&2
        start_services
        return 1
    fi
}

uninstall_units() {
    require_root
    stop_services || true
    disable_services
    if [[ "$DRY_RUN" -eq 0 ]]; then
        rm -f "$SYSTEMD_DIR/$MASTER_UNIT" "$SYSTEMD_DIR/$RUNTIME_TEMPLATE"
        systemctl daemon-reload
    else
        echo "DRY-RUN: remove WhiteLabel unit files (data preserved)."
    fi
    echo "OK: services removed; project data was preserved."
}

menu() {
    cat <<'EOF'

Hiddify WhiteLabel Operations
1. Install or update
2. Start services
3. Stop services
4. Restart services
5. Service status
6. Health check
7. Create encrypted backup
8. Restore encrypted backup
9. Remove systemd services (preserve data)
0. Exit
EOF
    read -r -p "Select option: " choice
    case "$choice" in
        1) ACTION=install ;; 2) ACTION=start ;; 3) ACTION=stop ;;
        4) ACTION=restart ;; 5) ACTION=status ;; 6) ACTION=health ;;
        7) ACTION=backup ;; 8) ACTION=restore ;; 9) ACTION=uninstall ;;
        0) exit 0 ;; *) echo "Invalid option." >&2; exit 2 ;;
    esac
}

[[ -n "$ACTION" ]] || menu
case "$ACTION" in
    install) install_all ;;
    start) require_root; start_services ;;
    stop) require_root; stop_services ;;
    restart) require_root; stop_services; start_services ;;
    status) status_services ;;
    health) health_services ;;
    backup) backup_action ;;
    restore) require_root; restore_action ;;
    uninstall) uninstall_units ;;
esac

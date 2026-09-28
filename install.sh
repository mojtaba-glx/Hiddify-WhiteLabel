#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
SYSTEMD_DIR="/etc/systemd/system"
MASTER_UNIT="hiddify-whitelabel-master.service"
RUNTIME_TEMPLATE="hiddify-whitelabel-runtime@.service"
MANAGER_COMMAND="/usr/local/bin/whitelabel"
UPDATE_REMOTE="${WL_UPDATE_REMOTE:-origin}"
UPDATE_BRANCH="${WL_UPDATE_BRANCH:-main}"
DRY_RUN=0
ACTION=""

for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        install|update|start|stop|restart|status|health|logs|backup|restore|migrate|settings|token|admin-id|shards|uninstall|uninstall-full|version)
            ACTION="$argument"
            ;;
        *) echo "Unknown argument: $argument" >&2; exit 2 ;;
    esac
done

version() {
    if [[ -f "$ROOT_DIR/VERSION" ]]; then
        tr -d '\r\n' < "$ROOT_DIR/VERSION"
    else
        echo "unknown"
    fi
}

service_user() {
    stat -c '%U' "$ROOT_DIR"
}

service_group() {
    id -gn "$(service_user)"
}

require_root() {
    if [[ "$DRY_RUN" -eq 0 && "${EUID:-$(id -u)}" -ne 0 ]]; then
        echo "ERROR: run this action with sudo/root." >&2
        exit 1
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: required command '$1' is missing." >&2
        return 1
    }
}

read_tty() {
    local __var="$1"; shift
    if [[ ! -r /dev/tty ]]; then
        echo "ERROR: interactive input requires a terminal." >&2
        return 1
    fi
    IFS= read -r "$@" "$__var" < /dev/tty
}

read_tty_secret() {
    local __var="$1" prompt="$2"
    if [[ ! -r /dev/tty ]]; then
        echo "ERROR: interactive input requires a terminal." >&2
        return 1
    fi
    IFS= read -r -s -p "$prompt" "$__var" < /dev/tty
    echo > /dev/tty
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

runtime_units_for_count() {
    local count="$1" index
    for ((index=0; index<count; index++)); do
        printf '%s\n' "hiddify-whitelabel-runtime@${index}.service"
    done
}

runtime_units() {
    runtime_units_for_count "$(shard_count)"
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

install_manager_command() {
    require_root
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "DRY-RUN: install $MANAGER_COMMAND -> $ROOT_DIR/install.sh"
        return
    fi
    local temporary="${MANAGER_COMMAND}.tmp.$$"
    cat > "$temporary" <<EOF
#!/usr/bin/env bash
exec "$ROOT_DIR/install.sh" "\$@"
EOF
    chmod 755 "$temporary"
    mv "$temporary" "$MANAGER_COMMAND"
}

ensure_directories() {
    local user group
    user="$(service_user)"
    group="$(service_group)"
    install -d -m 700 -o "$user" -g "$group"         "$ROOT_DIR/data" "$ROOT_DIR/runtime" "$ROOT_DIR/backups" "$ROOT_DIR/logs"
}

ensure_venv() {
    require_command python3
    if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
        as_service_user python3 -m venv "$ROOT_DIR/.venv"
    fi
    as_service_user "$ROOT_DIR/.venv/bin/python" -m pip install         --disable-pip-version-check -r "$ROOT_DIR/requirements.txt"
}

ensure_environment() {
    if [[ -f "$ENV_FILE" ]]; then
        chmod 600 "$ENV_FILE"
        chown "$(service_user)":"$(service_group)" "$ENV_FILE"
        return
    fi
    local master_token admin_id encryption_key temporary
    read_tty_secret master_token "MasterBot token (hidden): "
    read_tty admin_id -p "Master admin numeric Telegram ID: "
    if [[ "$master_token" != *:* || ! "$admin_id" =~ ^[1-9][0-9]*$ ]]; then
        unset master_token admin_id
        echo "ERROR: invalid token or admin ID." >&2
        return 1
    fi
    if ! verify_master_token_value "$master_token"; then
        unset master_token admin_id
        echo "ERROR: MasterBot token verification failed." >&2
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
    chown "$(service_user)":"$(service_group)" "$temporary"
    mv "$temporary" "$ENV_FILE"
    unset master_token admin_id encryption_key
    echo "OK: secure .env created."
}

edit_env_value() {
    local key="$1" value="$2" result
    export WL_ENV_VALUE="$value"
    if as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/env_edit.py" --env-file "$ENV_FILE" --key "$key"; then
        result=0
    else
        result=$?
    fi
    unset WL_ENV_VALUE
    chmod 600 "$ENV_FILE"
    chown "$(service_user)":"$(service_group)" "$ENV_FILE"
    return "$result"
}

verify_master_token_value() {
    local token="$1" result
    export WL_BOT_TOKEN="$token"
    if as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/verify_bot_token.py"; then
        result=0
    else
        result=$?
    fi
    unset WL_BOT_TOKEN
    return "$result"
}

install_units() {
    local user render_dir rollback_dir unit old runtime_unit old_count new_count max_count
    user="$(service_user)"
    render_dir="$ROOT_DIR/runtime/systemd"
    rollback_dir="$ROOT_DIR/runtime/install-rollback/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    install -d -m 700 -o "$user" -g "$(service_group)" "$render_dir" "$rollback_dir"
    printf '%s\n' "$rollback_dir" > "$ROOT_DIR/runtime/last-unit-rollback"
    chmod 600 "$ROOT_DIR/runtime/last-unit-rollback"

    new_count="$(shard_count)"
    old_count="$new_count"
    if [[ -f "$ROOT_DIR/runtime/installed-shard-count" ]]; then
        old_count="$(cat "$ROOT_DIR/runtime/installed-shard-count")"
        [[ "$old_count" =~ ^[1-9][0-9]*$ ]] || old_count="$new_count"
    fi
    printf '%s\n' "$old_count" > "$rollback_dir/old-shard-count"

    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/render_systemd.py"         --output-dir "$render_dir" --project-root "$ROOT_DIR" --service-user "$user"

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

    max_count="$new_count"
    (( old_count > max_count )) && max_count="$old_count"
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
    max_count="$new_count"
    (( old_count > max_count )) && max_count="$old_count"

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
    if [[ -f "$rollback_dir/$MASTER_UNIT.active" ]]; then
        systemctl start "$MASTER_UNIT" >/dev/null 2>&1 || true
    fi

    while IFS= read -r runtime_unit; do
        if [[ -f "$rollback_dir/$runtime_unit.enabled" ]]; then
            systemctl enable "$runtime_unit" >/dev/null 2>&1 || true
        else
            systemctl disable "$runtime_unit" >/dev/null 2>&1 || true
        fi
        if [[ -f "$rollback_dir/$runtime_unit.active" ]]; then
            systemctl start "$runtime_unit" >/dev/null 2>&1 || true
        fi
    done < <(runtime_units_for_count "$max_count")

    echo "ROLLBACK: previous systemd units and service state restored." >&2
}

start_services() {
    local unit
    run_systemctl start "$MASTER_UNIT"
    while IFS= read -r unit; do run_systemctl start "$unit"; done < <(runtime_units)
}

stop_services() {
    local unit
    while IFS= read -r unit; do run_systemctl stop "$unit" || true; done < <(runtime_units)
    run_systemctl stop "$MASTER_UNIT" || true
}

restart_services() {
    stop_services
    start_services
}

enable_services() {
    local unit
    run_systemctl enable "$MASTER_UNIT"
    while IFS= read -r unit; do run_systemctl enable "$unit"; done < <(runtime_units)
}

disable_services() {
    local unit
    while IFS= read -r unit; do run_systemctl disable "$unit" || true; done < <(runtime_units)
    run_systemctl disable "$MASTER_UNIT" || true
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

status_services() {
    local unit master_state
    echo
    echo "Hiddify WhiteLabel v$(version)"
    master_state="$(systemctl is-active "$MASTER_UNIT" 2>/dev/null || true)"
    printf 'MasterBot: %s\n' "${master_state:-unknown}"
    while IFS= read -r unit; do
        printf '%s: %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done < <(runtime_units)
}

health_services() {
    local unit failed=0
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/healthcheck.py"         --env-file "$ENV_FILE" || failed=1
    if [[ "$DRY_RUN" -eq 0 ]]; then
        systemctl is-active --quiet "$MASTER_UNIT" || {
            echo "ERROR: $MASTER_UNIT is not active."
            failed=1
        }
        while IFS= read -r unit; do
            systemctl is-active --quiet "$unit" || {
                echo "ERROR: $unit is not active."
                failed=1
            }
        done < <(runtime_units)
    fi
    return "$failed"
}

migrate_action() {
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/migrate.py"         --env-file "$ENV_FILE"
}

install_all() {
    require_root
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "DRY-RUN: create/repair venv, environment, database, systemd units and manager command."
        run_systemctl daemon-reload
        enable_services
        start_services
        return
    fi
    ensure_directories
    ensure_venv
    ensure_environment
    migrate_action

    trap rollback_units ERR
    install_units
    systemctl daemon-reload
    disable_obsolete_shards
    enable_services
    restart_services
    health_services
    printf '%s\n' "$(shard_count)" > "$ROOT_DIR/runtime/installed-shard-count"
    chmod 600 "$ROOT_DIR/runtime/installed-shard-count"
    install_manager_command
    trap - ERR

    echo
    echo "OK: Hiddify WhiteLabel v$(version) installed."
    echo "Manager command: sudo whitelabel"
}

run_project_tests() {
    as_service_user "$ROOT_DIR/.venv/bin/python" -m pytest -q
}

update_action() {
    require_root
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "DRY-RUN: fetch $UPDATE_REMOTE/$UPDATE_BRANCH, install dependencies, run tests, migrate, refresh units and restart."
        return
    fi
    require_command git
    [[ -d "$ROOT_DIR/.git" ]] || {
        echo "ERROR: this installation is not a Git checkout." >&2
        return 1
    }

    local dirty old_sha new_sha old_version new_version
    dirty="$(as_service_user git -C "$ROOT_DIR" status --porcelain --untracked-files=no)"
    if [[ -n "$dirty" ]]; then
        echo "ERROR: tracked project files have local changes; update aborted." >&2
        return 1
    fi

    old_sha="$(as_service_user git -C "$ROOT_DIR" rev-parse HEAD)"
    old_version="$(version)"
    as_service_user git -C "$ROOT_DIR" fetch "$UPDATE_REMOTE" "$UPDATE_BRANCH" --prune
    new_sha="$(as_service_user git -C "$ROOT_DIR" rev-parse "$UPDATE_REMOTE/$UPDATE_BRANCH")"
    new_version="$(as_service_user git -C "$ROOT_DIR" show "$new_sha:VERSION" 2>/dev/null | tr -d '\r\n' || echo unknown)"

    if [[ "$old_sha" == "$new_sha" ]]; then
        echo "Already up to date: v$old_version"
        install_all
        return
    fi

    echo "Updating v$old_version -> v$new_version"
    as_service_user git -C "$ROOT_DIR" checkout -q "$UPDATE_BRANCH"
    as_service_user git -C "$ROOT_DIR" reset --hard "$new_sha"

    # Update dependencies and validate the new code before stopping running services.
    ensure_venv
    if ! run_project_tests; then
        echo "ERROR: new version failed tests; restoring previous code." >&2
        as_service_user git -C "$ROOT_DIR" reset --hard "$old_sha"
        ensure_venv
        return 1
    fi

    stop_services
    migrate_action
    trap rollback_units ERR
    install_units
    systemctl daemon-reload
    disable_obsolete_shards
    enable_services
    start_services
    health_services
    printf '%s\n' "$(shard_count)" > "$ROOT_DIR/runtime/installed-shard-count"
    chmod 600 "$ROOT_DIR/runtime/installed-shard-count"
    install_manager_command
    trap - ERR
    echo "OK: update completed. Current version: v$(version)"
}

backup_action() {
    as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/backup.py"         --env-file "$ENV_FILE"
}

restore_action() {
    require_root
    local backup_file
    read_tty backup_file -p "Encrypted backup file: "
    stop_services
    if as_service_user "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/restore.py"         "$backup_file" --env-file "$ENV_FILE" --yes; then
        start_services
        health_services
    else
        echo "ERROR: restore failed; starting existing installation." >&2
        start_services
        return 1
    fi
}

change_master_token() {
    require_root
    local token backup
    read_tty_secret token "New MasterBot token (hidden): "
    if ! verify_master_token_value "$token"; then
        unset token
        echo "ERROR: token was not changed." >&2
        return 1
    fi
    backup="$ROOT_DIR/runtime/env-before-token-$"
    cp -a "$ENV_FILE" "$backup"
    chmod 600 "$backup"
    if ! edit_env_value MASTER_BOT_TOKEN "$token"; then
        rm -f "$backup"
        unset token
        return 1
    fi
    unset token
    run_systemctl restart "$MASTER_UNIT"
    sleep 2
    if systemctl is-active --quiet "$MASTER_UNIT"; then
        rm -f "$backup"
        echo "OK: MasterBot token updated."
    else
        echo "ERROR: MasterBot failed after token change; restoring previous .env." >&2
        cp -a "$backup" "$ENV_FILE"
        rm -f "$backup"
        run_systemctl restart "$MASTER_UNIT" || true
        return 1
    fi
}

change_admin_id() {
    require_root
    local admin_id
    read_tty admin_id -p "New Master admin numeric Telegram ID: "
    edit_env_value MASTER_ADMIN_ID "$admin_id"
    run_systemctl restart "$MASTER_UNIT"
    echo "OK: Master admin ID updated."
}

change_shards() {
    require_root
    local count
    read_tty count -p "Runtime shard count (1-64): "
    edit_env_value RUNTIME_SHARD_COUNT "$count"
    install_units
    systemctl daemon-reload
    disable_obsolete_shards
    enable_services
    restart_services
    printf '%s\n' "$(shard_count)" > "$ROOT_DIR/runtime/installed-shard-count"
    chmod 600 "$ROOT_DIR/runtime/installed-shard-count"
    health_services
    echo "OK: runtime shard count updated."
}

logs_menu() {
    require_root
    local choice
    cat <<'EOF'

========== Logs ==========
1) MasterBot - last 200 lines
2) MasterBot - live
3) TenantRuntime - last 200 lines
4) TenantRuntime - live
5) Errors from all WhiteLabel services
0) Back
EOF
    read_tty choice -p "Select: "
    case "$choice" in
        1) journalctl -u "$MASTER_UNIT" -n 200 --no-pager ;;
        2) journalctl -u "$MASTER_UNIT" -f ;;
        3) journalctl -u 'hiddify-whitelabel-runtime@*' -n 200 --no-pager ;;
        4) journalctl -u 'hiddify-whitelabel-runtime@*' -f ;;
        5) journalctl -u "$MASTER_UNIT" -u 'hiddify-whitelabel-runtime@*' -p warning -n 250 --no-pager ;;
        0) return 0 ;;
        *) echo "Invalid option." >&2; return 2 ;;
    esac
}

settings_menu() {
    require_root
    local choice
    cat <<'EOF'

======== Settings ========
1) Change MasterBot token
2) Change Master admin Telegram ID
3) Change Runtime shard count
0) Back
EOF
    read_tty choice -p "Select: "
    case "$choice" in
        1) change_master_token ;;
        2) change_admin_id ;;
        3) change_shards ;;
        0) return 0 ;;
        *) echo "Invalid option." >&2; return 2 ;;
    esac
}

uninstall_units() {
    require_root
    stop_services || true
    disable_services
    if [[ "$DRY_RUN" -eq 0 ]]; then
        rm -f "$SYSTEMD_DIR/$MASTER_UNIT" "$SYSTEMD_DIR/$RUNTIME_TEMPLATE"
        systemctl daemon-reload
    else
        echo "DRY-RUN: remove WhiteLabel systemd units; keep project/data."
    fi
    echo "OK: services removed; project and data preserved."
}

full_uninstall() {
    require_root
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "DRY-RUN: stop/disable services, remove units, manager command, project files and dedicated service user."
        return
    fi

    local confirm user
    echo
    echo "WARNING: this removes the application, database, .env, backups and logs."
    echo "Create/export any backup you need before continuing."
    read_tty confirm -p "Type DELETE ALL to continue: "
    [[ "$confirm" == "DELETE ALL" ]] || {
        echo "Cancelled."
        return 0
    }

    user="$(service_user)"
    stop_services || true
    disable_services || true
    rm -f "$SYSTEMD_DIR/$MASTER_UNIT" "$SYSTEMD_DIR/$RUNTIME_TEMPLATE" "$MANAGER_COMMAND"
    systemctl daemon-reload || true
    cd /
    rm -rf -- "$ROOT_DIR"
    if [[ "$user" == "whitelabel" ]] && id "$user" >/dev/null 2>&1; then
        userdel -r "$user" >/dev/null 2>&1 || userdel "$user" >/dev/null 2>&1 || true
    fi
    echo "OK: Hiddify WhiteLabel was completely removed."
}

main_menu() {
    local choice
    while true; do
        cat <<EOF

========================================
 Hiddify WhiteLabel v$(version)
========================================
1) Install / Repair
2) Update from GitHub
3) Restart all bots
4) Service status
5) Logs
6) Health check
7) Settings
8) Create encrypted backup
9) Restore encrypted backup
10) Run database migrations
11) Start all bots
12) Stop all bots
13) Remove services (keep data)
14) FULL uninstall
0) Exit
========================================
EOF
        read_tty choice -p "Select: "
        case "$choice" in
            1) install_all ;;
            2) update_action ;;
            3) require_root; restart_services ;;
            4) status_services ;;
            5) logs_menu ;;
            6) health_services ;;
            7) settings_menu ;;
            8) backup_action ;;
            9) restore_action ;;
            10) migrate_action ;;
            11) require_root; start_services ;;
            12) require_root; stop_services ;;
            13) uninstall_units ;;
            14) full_uninstall; return 0 ;;
            0) return 0 ;;
            *) echo "Invalid option." >&2 ;;
        esac
    done
}

dispatch() {
    case "$ACTION" in
        install) install_all ;;
        update) update_action ;;
        start) require_root; start_services ;;
        stop) require_root; stop_services ;;
        restart) require_root; restart_services ;;
        status) status_services ;;
        health) health_services ;;
        logs) logs_menu ;;
        backup) backup_action ;;
        restore) restore_action ;;
        migrate) migrate_action ;;
        settings) settings_menu ;;
        token) change_master_token ;;
        admin-id) change_admin_id ;;
        shards) change_shards ;;
        uninstall) uninstall_units ;;
        uninstall-full) full_uninstall ;;
        version) version; echo ;;
        "") main_menu ;;
    esac
}

dispatch

# Operations guide

Version: 0.6.0

## Requirements

- Linux with systemd
- Python 3.11 or newer and the `python3-venv` package
- A dedicated project owner account
- The project path must be absolute and contain no whitespace

Never place a real token in a command argument, shell history, ticket, or log.
The installer reads the MasterBot token with hidden terminal input and creates
`.env` with mode `0600`.

## Preview and install

```bash
cd /home/mojte/Hiddify-WhiteLabel
./install.sh --dry-run install
sudo ./install.sh install
```

The installer keeps existing `.env` values, installs pinned dependencies into
`.venv`, applies migrations, renders systemd units and starts:

- `hiddify-whitelabel-master.service`
- `hiddify-whitelabel-runtime@0.service` through
  `hiddify-whitelabel-runtime@N.service`

`N` is `RUNTIME_SHARD_COUNT - 1`. License jobs run inside MasterBot and do not
need a separate service.

If unit installation, startup, or health validation fails, the installer stops
the new services and restores the previous unit files and their recorded
enable/active state. Rollback snapshots are private under
`runtime/install-rollback/`.

## Routine commands

```bash
sudo ./install.sh start
sudo ./install.sh stop
sudo ./install.sh restart
./install.sh status
./install.sh health
```

The interactive menu exposes the same actions. `health` validates the local
configuration/database and confirms every configured systemd instance is
active.

## Encrypted backup

```bash
./install.sh backup
```

Enter a new passphrase twice. The resulting `backups/*.wlbak` file is mode
`0600`; its database and `.env` are not readable without that passphrase. Keep
the passphrase outside the server. A lost passphrase cannot be recovered.

For automation, put only the passphrase in a mode-`0600` file and run:

```bash
.venv/bin/python scripts/backup.py --passphrase-file /secure/path/backup.pass
```

Do not store the passphrase file inside the project or backup directory.

## Restore

```bash
sudo ./install.sh restore
```

The installer stops all WhiteLabel services before calling the restore tool.
Restore rejects an unauthenticated, modified, publicly readable, structurally
invalid, or migration-incompatible archive. Before replacement it writes the
current database and `.env` to a private `runtime/rollback/` directory.

If `.env` is missing or damaged, use the direct recovery command and explicitly
provide the database destination when it differs from the default:

```bash
sudo .venv/bin/python scripts/restore.py /secure/backup.wlbak \
  --database /home/mojte/Hiddify-WhiteLabel/data/whitelabel.db --yes
```

Then run `sudo ./install.sh start` and `./install.sh health`.

## Manual rollback

If an application update fails but systemd rollback has already restored the
old unit files:

1. Stop services with `sudo ./install.sh stop`.
2. Restore the last known-good encrypted backup.
3. Check the project `.env` and `VERSION` without printing secret values.
4. Start services with `sudo ./install.sh start`.
5. Run `./install.sh health` and inspect `journalctl` for the affected unit.

Removing units preserves all project data:

```bash
sudo ./install.sh uninstall
```

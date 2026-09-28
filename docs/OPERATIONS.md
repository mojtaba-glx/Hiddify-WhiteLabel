# Operations guide

Version: 0.9.3

## Supported host

The one-line bootstrap currently targets Ubuntu/Debian servers with systemd
and `apt`. It installs Git, curl, CA certificates, Python, venv support and
pip automatically.

The bootstrap creates a dedicated unprivileged system account named
`whitelabel`, clones the project under `/opt/hiddify-whitelabel`, and runs
all Telegram application services as that account. The service processes do
not run as root.

## One-line install

For a fresh server:

```bash
curl -fsSL https://raw.githubusercontent.com/mojtaba-glx/Hiddify-WhiteLabel/main/bootstrap.sh | sudo bash
```

The installer asks interactively for:

- MasterBot token (hidden input)
- Master admin numeric Telegram ID

The encryption key is generated locally and never printed. Runtime data,
database files, backups and logs are private on disk.

After installation, open the operations manager with:

```bash
sudo whitelabel
```

The command points to the current project's `install.sh`, so installer/menu
updates are picked up automatically when the application is updated.

## Terminal manager

The main menu provides:

1. Install / Repair
2. Update from GitHub
3. Restart all bots
4. Service status
5. Logs
6. Health check
7. Settings
8. Encrypted backup
9. Encrypted restore
10. Database migrations
11. Start all bots
12. Stop all bots
13. Remove systemd services while preserving project/data
14. Full uninstall

The Settings submenu shows the current non-secret configuration and allows the
operator to change the MasterBot token, Master admin Telegram ID, runtime shard
count and display timezone. Secret token input is hidden and the helper updates
`.env` atomically without printing the token.

The Logs submenu provides recent and live journal output for MasterBot and
TenantRuntime shards plus a combined warnings/errors view.

## Direct commands

Every important menu operation can also be called directly:

```bash
sudo whitelabel update
sudo whitelabel restart
sudo whitelabel status
sudo whitelabel health
sudo whitelabel logs
sudo whitelabel settings
sudo whitelabel backup
sudo whitelabel restore
sudo whitelabel migrate
sudo whitelabel token
sudo whitelabel admin-id
sudo whitelabel shards
sudo whitelabel timezone
sudo whitelabel version
```

## Safe update flow

`sudo whitelabel update`:

1. Refuses to overwrite tracked local source changes.
2. Fetches `origin/main`.
3. Updates the checkout.
4. Installs pinned Python requirements.
5. Runs the complete offline pytest suite **before downtime**.
6. If tests fail, restores the previous source commit.
7. Creates a private consistent SQLite + `.env` rollback snapshot.
8. Stops the running bots only after tests and snapshot creation succeed.
9. Applies pending database migrations.
10. Re-renders systemd units and shard instances.
11. Starts services and runs health checks.
12. If migration, startup or health validation fails, restores the pre-update
    database, environment and source commit, re-renders the old units and
    starts the previous version again.

Successful updates retain the private rollback snapshot under
`runtime/update-rollback/`. This snapshot is for local emergency rollback;
it is not a substitute for the encrypted off-server backup.

## Service model

The installer manages:

- `hiddify-whitelabel-master.service`
- `hiddify-whitelabel-runtime@0.service` through the configured shard count

License jobs run inside MasterBot; there is no separate license daemon.

Changing the shard count from the menu re-renders units, disables obsolete
instances, restarts the runtime set, and finishes with a health check.

## Encrypted backup

```bash
sudo whitelabel backup
```

Enter a backup passphrase twice. The resulting `backups/*.wlbak` contains a
consistent SQLite snapshot and the environment file, authenticated and
encrypted by the existing backup subsystem.

Keep the backup passphrase outside the server. It cannot be recovered if lost.

## Restore

```bash
sudo whitelabel restore
```

Restore stops all bot services first, authenticates and validates the backup,
checks SQLite/migrations, saves a private rollback snapshot, restores the
database/environment atomically, starts all bots and runs health checks.

## Token changes

```bash
sudo whitelabel token
```

The new token is hidden while typing. Before editing, the installer saves a
private temporary copy of `.env`. If MasterBot does not remain active after
restart, the old environment is restored automatically.

Tenant AdminBot/UserBot tokens are tenant-owned credentials and remain managed
through the platform provisioning/runtime flows; this terminal action changes
only the platform MasterBot token.

## Removal

To remove only systemd services and preserve data:

```bash
sudo whitelabel uninstall
```

For a complete removal, including project files, SQLite database, `.env`,
backups and logs:

```bash
sudo whitelabel uninstall-full
```

Full uninstall requires typing the exact phrase `DELETE ALL`. The installer
also removes the dedicated `whitelabel` system account when that account owns
the installation.

## Dry-run checks

The repository keeps safe non-destructive previews for installer development:

```bash
./install.sh --dry-run install
./install.sh --dry-run update
./install.sh --dry-run restart
./install.sh --dry-run uninstall-full
```

Dry-run actions never require root and never print secret values.

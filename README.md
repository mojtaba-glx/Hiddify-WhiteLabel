# Hiddify-WhiteLabel

**Current code version:** `1.3.18` (see [VERSION](VERSION))  
**Project status:** Active development — final live-provider acceptance is still in progress.

Hiddify-WhiteLabel is a multi-tenant platform for operating branded VPN subscription businesses through Telegram. A platform owner manages tenants and licenses with **MasterBot**. Each tenant is provisioned with its own **AdminBot** and **UserBot**, while tenant bots run in a shared, sharded runtime instead of creating a separate operating-system process for every tenant.

> This repository is under active development. The automated offline test and release checks do not replace the documented live staging and rollback acceptance process. See [Project status](docs/PROJECT_STATUS.md) before treating a provider integration as production-accepted.

## Features

- **Platform management:** tenant provisioning, tenant lifecycle, plans, licenses, license expiry/grace handling, audit history and operational alerts.
- **Tenant isolation:** tenant-scoped data access, role-specific bot routing, per-tenant runtime state and access checks.
- **Telegram storefront:** plan catalog, purchase and renewal flows, payment instructions, receipt review, wallet operations, coupons, referrals and one-time trials.
- **Tenant operations:** customer and subscription management, support tickets, broadcast tools, dashboards and reporting.
- **VPN panel integrations:** provider adapters for Hiddify Manager, X-UI Sanaei, X-UI Alireza and X-NET.
- **Multi-node subscriptions:** primary and child-node mappings, aggregated usage, managed subscription links and periodic lifecycle enforcement.
- **Operations:** Ubuntu/Debian bootstrap, systemd services, health checks, encrypted backup/restore and guarded update rollback.

The exact feature and provider acceptance status is tracked in [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) and [docs/STAGED_RELEASE_DRILL.md](docs/STAGED_RELEASE_DRILL.md).

## Architecture

```text
Hiddify-WhiteLabel/
├── MasterBot/          # Platform owner interface and customer storefront
├── TenantRuntime/      # Shared, sharded tenant bot runtime
│   ├── AdminBot/       # Tenant administration workflows
│   └── UserBot/        # End-user purchase and support workflows
├── Gateway/            # Bot catalog, routing and runtime policy
├── LicenseService/     # License lifecycle and durable notifications
├── Provisioning/       # Tenant and bot provisioning
├── Database/           # SQLite connection and repository layer
├── Migrations/         # Versioned SQL schema migrations
├── Shared/             # Settings, cryptography, access and redacted logging
├── Ops/                # Health checks, backups and systemd helpers
├── Tests/              # Offline automated test suite
├── scripts/            # Development, migration and release tools
└── docs/               # Architecture, operations, threat model and decisions
```

### Runtime model

Tenant bots are assigned to a fixed number of runtime shards. Each update is checked against the current bot route, tenant status, runtime readiness, tenant ownership (for AdminBot) and license state. Durable conversation state is scoped by tenant, bot role and Telegram user.

### Data storage

The current implementation uses **SQLite**. PostgreSQL support is not implemented. Do not assume that the current codebase is PostgreSQL-ready.

## Provider integrations

The repository contains provider adapters for:

- Hiddify Manager
- X-UI Sanaei
- X-UI Alireza
- X-NET

Provider adapters and offline contract tests are present, but a provider must not be described as fully live-validated until it has passed the disposable staging drill for the same release candidate. The remaining acceptance requirements are documented in [STAGED_RELEASE_DRILL.md](docs/STAGED_RELEASE_DRILL.md).

## Development setup

Use Python 3.10 or 3.12, matching the versions configured in the GitHub Actions workflow.

```bash
git clone https://github.com/mojtaba-glx/Hiddify-WhiteLabel.git
cd Hiddify-WhiteLabel

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

pytest -q
python scripts/release_drill.py
```

The automated test suite and release drill are designed to run offline with test fixtures. They do not require production bot tokens, panel credentials or customer data.

For a complete test run and the staged acceptance process, see:

- [Operations guide](docs/OPERATIONS.md)
- [Staged release drill](docs/STAGED_RELEASE_DRILL.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- [Dependency and third-party license review](docs/DEPENDENCIES.md)

## Installation

The bootstrap flow currently targets Ubuntu/Debian servers with systemd.

```bash
curl -fsSL https://raw.githubusercontent.com/mojtaba-glx/Hiddify-WhiteLabel/main/bootstrap.sh | sudo bash
```

The installer creates a dedicated service account, prepares the runtime environment, applies database migrations, installs systemd units and runs health checks. Review the scripts and [Operations guide](docs/OPERATIONS.md) before using the installer on any server.

**Do not test installation, updates, provider integrations or rollback against production customer accounts.** Use a staging host, disposable panel users and a verified backup.

## Security notes

- Runtime secrets are supplied through a private environment file; `.env` is excluded by `.gitignore`.
- Tenant bot tokens and panel credentials are encrypted before persistent storage.
- Webhook secrets are stored as hashes.
- Logs use redaction helpers for credential-like values.
- Database files, runtime data, logs and backup artifacts are excluded by the repository ignore rules.

These are design and repository-level controls, not a substitute for an independent security review. Never commit real bot tokens, panel credentials, encryption keys, backup passphrases, databases, customer exports or production configuration.

## Language

The current Telegram bot interface is Persian. This repository's developer-facing documentation is maintained in English. English localization of bot menus and messages is not included in this documentation-only release preparation.

## License

A license has not yet been selected for this repository. Until a license is added, the source is not offered under an open-source license. See [Project status](docs/PROJECT_STATUS.md) for release-readiness items.

## Contributions and issue reports

Bug reports and reproducible test cases are welcome. Please do not include real tokens, panel credentials, customer identifiers, subscription links or production logs in issues. Redact sensitive values before sharing diagnostic output.

# Project Status and Release Readiness

**Repository:** `mojtaba-glx/Hiddify-WhiteLabel`  
**Version recorded in `VERSION`:** `1.3.18`  
**Status:** Active development; final live-provider acceptance is incomplete.

This document distinguishes implemented code and offline checks from operational validation that still requires a staging environment.

## Implemented areas

The current source tree includes:

- MasterBot platform management and tenant/customer-facing workflows.
- Tenant provisioning and license lifecycle services.
- A shared, sharded TenantRuntime with separate AdminBot and UserBot handler packages.
- Tenant-scoped database repositories, state and routing policy.
- Provider adapters for Hiddify Manager, X-UI Sanaei, X-UI Alireza and X-NET.
- Purchase, renewal, receipt review, wallet, coupon, referral, trial and support workflows.
- Multi-node subscription mappings, usage aggregation, managed subscription links and lifecycle enforcement.
- Installer, systemd integration, health checks, encrypted backup/restore and update rollback tooling.
- An offline automated test suite and a scripted release drill.

## Acceptance still in progress

The roadmap marks Phase 8 as partial. The automated offline release gate exists, but the operator-run live staging drill and rollback record remain outstanding.

Before describing a provider as production-validated, run the staging procedure in `docs/STAGED_RELEASE_DRILL.md` against disposable test users and record the result for the same candidate commit. A provider that has not been tested must be reported as **not executed**, not as passed.

The presence of adapter code or offline tests does not, by itself, prove compatibility with every panel version or deployment configuration.

## Known technical scope

- **Database:** SQLite only. PostgreSQL adapter and PostgreSQL migration set are not implemented.
- **Runtime:** shared sharded tenant processes; not one operating-system process per tenant.
- **CI configuration:** GitHub Actions is configured for Python 3.10 and 3.12, runs `pytest`, the release-plan check and shell syntax checks. This file does not claim that a particular workflow run passed.
- **User interface:** current Telegram bot menus and messages are Persian. The developer documentation is English. English bot localization is not part of this release-preparation change.

## Open-source publication requirements

The following items must be resolved before presenting this repository as a fully licensed open-source project:

1. **Choose and add a license.** There is currently no `LICENSE` file. Do not claim an OSI-approved license until the rights holder has chosen and added one.
2. **Confirm code provenance and attribution.** A source comment in `TenantRuntime/UserBot/qr_and_configs.py` identifies subscription/config delivery code as ported from `Hiddify-SellBot`. Confirm ownership and permission for all reused or adapted material, then add accurate attribution where required.
3. **Complete a full history secret scan.** Review all branches, tags and Git history with GitHub secret scanning or an equivalent local scanner before making the repository public. A clean current working tree does not prove that an old commit contains no exposed credential.
4. **Complete live staging acceptance.** Record provider, rollback and operational results for the release candidate to be published.
5. **Review public examples and documentation.** Confirm that all URLs, sample values, screenshots and test fixtures are intentionally public and contain no customer or infrastructure data.

## Security review scope

The current repository tree and selected configuration, test, provider, provisioning, backup and documentation files were inspected during preparation. No obvious live Telegram bot token, private key, production database or backup artifact was identified in the inspected current files. Token-shaped values found in test fixtures are explicitly fake test values.

This was a repository-level spot check, not a complete forensic audit of every historical object, branch, tag, workflow artifact or external deployment. GitHub Secret Scanning alerts could not be retrieved through the available connection. Do not treat this note as a guarantee that the entire Git history is free of secrets.

## Publication rule

Do not switch repository visibility to public until the license, code-provenance review and full-history secret scan are complete. Keep production deployment details, customer data and real credentials private.

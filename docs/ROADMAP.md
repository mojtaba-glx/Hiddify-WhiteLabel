# Roadmap

- [x] **Phase 0 — Design** (this snapshot): read-only study of reference,
  `ARCHITECTURE.md`, `THREAT_MODEL.md`, `ROADMAP.md`, `decisions/` ADRs.
- [x] **Phase 1 — Independent infra** (this snapshot): scaffold, secure
  settings, redacted logger, UTC helpers, token-crypto interface, schema +
  migration v1, repositories, license state machine, offline tests green.
- [x] **Phase 2 — MasterBot UI**: global access gate, main menu
  (👥 مشتریان، 🔐 لایسنس‌ها، 📦 پلن‌ها، 📊 آمار، ⚠️ هشدارها، 🧾 تاریخچه، ⚙️ تنظیمات),
  tenant/plan CRUD, secure token registration + mocked `getMe`, renew /
  suspend / reactivate with confirm, pagination + search, audit view.
- [x] **Phase 3 — LicenseService jobs**: evaluator, grace, 7/3/1-day
  scheduler with dedupe, short cache, safe behavior when master is down,
  runtime gate interface.
- [x] **Phase 4 — Tenant provisioning**: create tenant, register two
  tokens, per-tenant data space, per-bot webhook secret, disable/enable
  without data loss.
- [x] **Phase 5 — Gateway + runtime**: encrypted token→tenant catalog,
  fixed-count sharded polling, per-update tenant/owner/license gate, durable
  state key `tenant_id+role+user`, cross-tenant guards, and wired tenant-specific
  AdminBot/UserBot runtime shells.
- [x] **Phase 6 — Install & ops**: English-terminal `install.sh`, hardened
  MasterBot and sharded-runtime systemd units, duplicate-process locks,
  health checks, encrypted backup/validated restore, automatic unit rollback
  and documented recovery procedures.
- [x] **Phase 7 — Customer portal and commerce**: role-based PlatformBot
  menu, public plans, one-time trials, card-to-card/crypto method settings,
  photo/reference receipts, owner review, wallet top-up/spending, customer
  renewal, and paid-order provisioning. Customer callbacks and all customer
  data reads are actor-scoped; order totals are database-authoritative.
- [~] **Phase 8 — Tenant product modules + staged acceptance**: the
  tenant-scoped product is implemented for Hiddify Manager, X-UI
  Sanaei/Alireza and X-NET, including renewal/expiry/usage sync, multi-node
  managed smart subscriptions, reminder/global enforcement, reports and
  AdminBot/UserBot management, wallet/coupons/referrals/free trials.
  The automated offline release gate is now implemented in
  `scripts/release_drill.py`. The remaining acceptance item is the
  operator-run real-panel staging drill and rollback record described in
  `docs/STAGED_RELEASE_DRILL.md`. Phase 8 stays partial until every advertised
  provider and the operational rollback drill pass on the same release
  candidate.

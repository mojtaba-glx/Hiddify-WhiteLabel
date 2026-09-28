# MasterBot — Phases 2–4

The platform owner's Telegram interface is implemented in this package.

- `main.py`: settings, migration/connection wiring and polling entry point.
- `handlers.py`: global owner gate, menus, input flows and confirmation steps.
- `service.py`: owner-checked tenant, plan, license, bot, statistics, warning
  and audit use cases.
- `telegram_api.py`: mockable Telegram `getMe` token verifier.
- `views.py`: pure text and inline-keyboard rendering.

`main.py` also schedules the Phase-3 `LicenseJobRunner` through the Telegram
JobQueue. Delivery failures remain in the durable notification queue.

Run from the repository root after configuring `.env`:

```bash
python3 -m MasterBot
```

Tenant bot tokens are accepted only in the dedicated token flow, verified by
Telegram, encrypted immediately and never placed in conversation state or
logs. Suspending or archiving records does not delete their data.

The full-provisioning flow verifies AdminBot and UserBot in separate token
messages. Each message is deleted immediately; only the encrypted AdminBot
draft is retained between steps. The final transaction creates the tenant,
runtime namespace and both bot rows together. One-time webhook secrets are
sent only to the globally authorized owner with protected-content enabled.

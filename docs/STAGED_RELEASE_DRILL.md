# Staged Release Drill

This is the final acceptance gate for the tenant product integration work.
It is split into an automated **offline gate** and an operator-run **live
staging drill**. The live drill must use disposable test users on test panels;
never point it at production customer accounts.

## 1. Automated offline gate

Run the critical regression gate:

```bash
python3 scripts/release_drill.py
```

For the complete offline suite:

```bash
python3 scripts/release_drill.py --full-suite
```

To inspect the deterministic plan without executing commands:

```bash
python3 scripts/release_drill.py --plan-only
python3 scripts/release_drill.py --plan-only --json
```

The automated gate has no network step. It covers:

- Hiddify, X-UI Sanaei/Alireza and X-NET provider contracts.
- purchase/renewal/expiry/usage lifecycle.
- multi-node and managed smart subscriptions.
- reminder/global-enforcer regressions.
- reports, AdminBot/UserBot flows, wallet/coupon/referral/free-trial paths.
- installer/update rollback and health regression tests.
- Bash syntax for `install.sh` and `bootstrap.sh`.
- a fresh database migration smoke test in a private temporary directory.

A failed step stops the drill immediately. No panel or Telegram secret is read
by the automated gate.

## 2. Live staging prerequisites

Use a separate staging host and disposable panel users. Before starting:

- CI for the exact candidate commit is green on Python 3.10 and 3.12.
- encrypted backup/restore has been tested on the staging WhiteLabel host.
- Hiddify test target has the intended supported manager version.
- Sanaei, Alireza and X-NET targets use test inbounds only.
- panel credentials belong to staging and are encrypted through the normal
  Tenant AdminBot flow.
- the smart-subscription public URL points to staging.
- no production tenant, production bot token or production user is selected.

If one provider is not available, record it as **not executed**; do not mark
Phase 8 accepted until every provider that will be advertised as supported has
passed at least once on a real disposable target.

## 3. Per-provider live drill

For Hiddify, Sanaei X-UI, Alireza X-UI and X-NET, record the candidate commit
SHA and perform this sequence with a new disposable subscription:

1. Configure the server and encrypted credential through Tenant AdminBot.
2. Create a small test plan and purchase/provision one service.
3. Verify the provider user exists and the native subscription link opens.
4. If multi-node is enabled, verify every expected child mapping exists.
5. Verify the managed smart link returns deduplicated usable configs.
6. Generate a small amount of traffic and verify usage/last-online sync.
7. Renew once and verify quota reset/expiry extension without duplicate users.
8. Disable then re-enable before expiry and verify provider state converges.
9. Force a short expiry/quota test and verify Global Enforcer disables every
   current mapping before local state becomes `expired`.
10. Confirm the expiry notice/reminder is not duplicated by a repeated scan.
11. Delete the disposable service and verify remote mappings are removed or,
    where the provider cannot delete, verified disabled according to adapter
    policy.

Retry one remote operation after intentionally interrupting it once. The retry
must converge on the same logical subscription and must not create a duplicate
user or reset post-renewal traffic a second time.

## 4. Commerce and tenant-boundary drill

On a staging tenant:

- purchase once by receipt and once by full wallet payment.
- apply a coupon, reject/cancel an unpaid order and verify the reservation is
  released.
- top up the wallet, approve the receipt once, then verify a repeated approval
  cannot double-credit.
- use a referral link and verify only the allowed reward is credited once.
- claim the one-time free trial and verify a second claim is refused.
- verify customer search/reporting stays inside the tenant.
- verify a blocked customer cannot create a new order.
- verify an expired customer-created service requires renewal rather than
  reactivation.

## 5. Operations/rollback drill

On the staging WhiteLabel host:

1. Create an encrypted backup and validate restore.
2. Run `sudo whitelabel update` with a known-good candidate.
3. Confirm tests finish before service downtime.
4. Confirm migrations, systemd rendering, startup and health checks succeed.
5. Exercise one controlled failed candidate in staging and verify the automatic
   rollback restores source, database and environment together.
6. Re-run the offline release gate after rollback.

## 6. Acceptance record

Keep one short record per candidate:

```text
candidate_sha:
ci_python_3_10: pass/fail
ci_python_3_12: pass/fail
offline_release_drill: pass/fail
hiddify: pass/fail/not-executed
xui_sanaei: pass/fail/not-executed
xui_alireza: pass/fail/not-executed
xnet: pass/fail/not-executed
multi_node_smart_sub: pass/fail
renew_expiry_usage: pass/fail
reminder_global_enforcer: pass/fail
commerce_wallet_coupon_referral_trial: pass/fail
backup_restore_update_rollback: pass/fail
notes:
```

Phase 8 can move from partial to complete only when the advertised provider
matrix and the operational rollback drill are all recorded as passing for the
same release candidate.

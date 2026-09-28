# ADR-0005 — Explicit license state machine with atomic audited transitions

- Status: accepted
- Date (UTC): 2026-09-13

## Context

License status drives billing access; ad-hoc updates cause split-brain
(active in one place, expired in another) and unaudited changes.

## Decision

- States: `pending, active, grace, suspended, expired, cancelled`
  (`LicenseService/states.py`), transition table `ALLOWED_TRANSITIONS`,
  pure `can_transition()` + `compute_effective_status()`.
- Writes are atomic: `UPDATE licenses SET status=? … WHERE id=? AND
  status=?` plus audit insert in the same SQLite transaction; the
  writer checks `cursor.rowcount` and rolls back otherwise.
- Expiry is evaluated, never assumed from callback data; suspension
  keeps all rows (no deletes); renew re-activates with new dates.

## Consequences

- Illegal transitions fail loudly and are covered by tests.
- The Phase-3 scheduler and runtime gate build on the same pure functions.
  Warning keys include an expiry-period fingerprint so renewal cannot suppress
  the next warning sequence or deliver a stale queued warning.

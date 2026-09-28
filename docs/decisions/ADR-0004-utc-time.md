# ADR-0004 — UTC storage, Asia/Tehran display

- Status: accepted
- Date (UTC): 2026-09-13

## Context

Mixed local times break expiry/grace math and DST handling.

## Decision

- Every timestamp persisted as ISO-8601 UTC (`datetime.timezone.utc`).
- `Shared/timeutils.py`: `utcnow()`, `ensure_utc()`, `iso_utc()`,
  `parse_utc()`, `format_tehran()` (stdlib `zoneinfo`, no extra dep).
- License math compares aware UTC datetimes only; presentation converts.

## Consequences

- Deterministic tests via injected `now`.
- Display timezone is configurable but storage never is.

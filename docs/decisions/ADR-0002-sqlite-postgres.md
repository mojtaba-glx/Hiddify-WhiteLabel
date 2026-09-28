# ADR-0002 — SQLite for the current Phase-1 implementation

- Status: accepted
- Date (UTC): 2026-09-13

## Context

Local dev/tests need zero-ops storage. PostgreSQL is the intended future
production store, but no Postgres adapter exists yet.

## Decision

- The current implementation is **SQLite only** (`sqlite3` stdlib),
  `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout`, explicit
  autocommit connections with `BEGIN IMMEDIATE` transactions.
- The schema (**Migrations/*.sql**) and the repository layer are written
  in broadly portable SQL (standard column types, `?` placeholders, FK
  constraints, no SQLite-only functions in queries), so a future
  PostgreSQL port is *feasible* — but it is **not** drop-in.
- A PostgreSQL adapter and a **separate** PostgreSQL migration set are
  future work; they are not part of this snapshot.

## Consequences

- Tests run offline against tmp-file SQLite.
- The `Database/` connection module is the single seam where a Postgres
  driver would be introduced. Until that adapter lands, any claim of
  "PostgreSQL-ready" is aspirational, not delivered.

# ADR-0001 — Monorepo for logically independent components

- Status: accepted
- Date (UTC): 2026-09-13

## Context

`MasterBot`, `Gateway`, `LicenseService`, `TenantRuntime` must evolve
independently, but license semantics and migrations must stay in sync
across them.

## Decision

Keep them in one repository (`Hiddify-WhiteLabel/`) with strict package
boundaries (`MasterBot/`, `Gateway/`, `LicenseService/`,
`TenantRuntime/`, `Shared/`, `Database/`). No cross-imports except
through `Shared/`, `Database/` and the documented `LicenseService`
gate interface.

## Consequences

- Single `VERSION` + ordered `Migrations/`; one test command.
- Extraction to separate services later remains possible; package
  boundaries are the seam.

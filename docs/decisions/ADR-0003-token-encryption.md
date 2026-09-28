# ADR-0003 — Fernet token encryption behind an interface

- Status: accepted
- Date (UTC): 2026-09-13

## Context

Bot tokens must never sit in plain text in DB/logs/exceptions, but the
cipher must stay testable and replaceable (e.g. KMS later).

## Decision

- `Shared/crypto.py` exposes `TokenCipher` ABC (`encrypt`/`decrypt`).
- Production impl `FernetTokenCipher` (package `cryptography`, pinned)
  with key from `TOKEN_ENCRYPTION_KEY` (file `0600`, outside DB).
- DB stores `encrypted_token` (Fernet string) + SHA-256
  `token_fingerprint` (UNIQUE) + `token_tail` (last-4 display fragment)
  for humans. `telegram_bot_id` (from getMe) is UNIQUE when set.
- Per-bot `webhook_secret` is never persisted raw: only its SHA-256
  `webhook_secret_hash` is stored, and verification uses
  `hmac.compare_digest` (constant time).
- `__repr__` of cipher/settings/models never includes key material.

## Consequences

- Crypto tests use ephemeral keys; repo tests use fake tokens only.
- Key rotation needs a re-encryption migration (deferred, documented).

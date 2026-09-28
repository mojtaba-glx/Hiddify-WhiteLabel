# LicenseService — Phase 3

- `states.py` defines deterministic lifecycle and warning-stage functions.
- `service.py` provides atomic audited transitions, renewal and the TTL cache.
- `jobs.py` evaluates all current licenses, persists expiry-specific warning
  events, claims deliveries, retries failures and recovers stale leases.
- `runtime.py` exposes the fail-closed `TenantRuntimeGate` used by the future
  shared TenantRuntime.
- `telegram_sender.py` sends safe warning summaries to the platform owner.

If MasterBot or Telegram is offline, notification rows remain retryable. The
runtime gate continues enforcing stored expiry and grace timestamps directly;
it does not require the scheduler process to be alive.

Delivery is intentionally at-least-once. A process crash in the very small
window after Telegram accepts a message but before the database marks it sent
can produce one duplicate warning. The durable event key prevents normal
reruns, concurrent workers and license scans from sending duplicates.

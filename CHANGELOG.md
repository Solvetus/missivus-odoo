# Changelog

All notable changes to Missivus for Odoo. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-08-26

Inbound mail. The addon is now the complete mail story for Odoo 19 Community on Microsoft 365:
send and receive through Microsoft Graph with application permissions and shared mailboxes.

### Added

- "Missivus — Microsoft Graph" server type on Incoming Mail Servers: fetches unread mail from a
  shared mailbox folder through Graph and hands the raw MIME to Odoo's native `message_process`
  (aliases, threading, catchall and bounce handling unchanged). Rides Odoo's existing fetchmail
  cron — no new cron, no relay, no extra dependency.
- Post-processing modes: mark as read (default) or move to a named folder (created on demand).
  Batch cap per run (default 50).
- Quarantine: a message Odoo cannot process is moved to the "Missivus Quarantine" folder
  (created on first use) and logged with its Graph message id and the exception class — never
  its content. The run continues with the remaining messages; a failed quarantine move leaves
  the message in place for the next run.
- Transient Graph/network errors abort the run cleanly; unprocessed messages stay unread.
- Graph client: list unread, fetch raw MIME, mark read, move, resolve/ensure folder, sharing
  the token cache, the 401 re-acquire and the error taxonomy of the send path.
- README: inbound walkthrough, `Mail.ReadWrite` application permission, and a troubleshooting
  section (alias domain / default From on Odoo 17+, `ErrorSendAsDenied`,
  `Test-ApplicationAccessPolicy`).

### Changed

- The four app-registration fields, their validation and the token test moved to a shared
  `missivus.graph.mixin` used by both Outgoing and Incoming Mail Servers. Field names and DB
  columns are unchanged — no migration.
- Manifest version 19.0.0.2.0.

## [0.1.0] — 2026-08-25

First release. The fifth Missivus platform, after
[Matomo](https://github.com/Solvetus/missivus-matomo),
[WordPress](https://github.com/Solvetus/missivus-wordpress),
[Nextcloud](https://github.com/Solvetus/missivus-nextcloud) and
[Ghost](https://github.com/Solvetus/missivus-ghost) — and the first Python member of the family.

### Added

- `missivus_mail_graph` addon for Odoo 19 Community: a "Missivus — Microsoft Graph" authentication
  type on Outgoing Mail Servers that sends every outbound message through Microsoft Graph
  `sendMail` (raw MIME) with application permissions and a shared mailbox.
- Client-credentials token cache (per tenant/client, 120 s expiry margin, thread-safe, one
  re-acquire on 401).
- Permanent vs transient error mapping; transient failures (429, 5xx, network) are re-queued on
  Odoo's own mail cron with 2/4/8/16/32-minute backoff or the `Retry-After` value, five retries.
- "Test Connection" acquires a token and reports the Microsoft Entra error description.
- `from_filter` pinned to the sender mailbox on save so Odoo's standard routing picks the server.
- Docker Compose dev environment, ruff + Odoo test suite in GitHub Actions.

[0.1.0]: https://github.com/Solvetus/missivus-odoo/releases/tag/v0.1.0
[0.2.0]: https://github.com/Solvetus/missivus-odoo/releases/tag/v0.2.0

# Changelog

All notable changes to Missivus for Odoo. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] — Unreleased

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

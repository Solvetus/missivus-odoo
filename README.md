<p align="center">
  <a href="https://missivus.com"><img src=".github/mark.svg" alt="Missivus" width="40" height="41"></a>
</p>

# Missivus for Odoo

**Send and receive Odoo mail through Microsoft Graph with application permissions and shared
mailboxes. No SMTP, no IMAP, no user login, no pip dependencies. Free, LGPLv3.**

## The problem

Microsoft retired Basic authentication for SMTP on Microsoft 365. The classic Odoo Outgoing Mail
Server — SMTP host, username, password — no longer works against Exchange Online, and the
"SMTP AUTH per mailbox" workarounds are on borrowed time.

Odoo Community's only Microsoft path is the built-in `microsoft_outlook` module, and it is
**delegated** OAuth: a person clicks *Connect your Outlook account*, the token belongs to that
person, mail goes out as them, and it stops when they leave, rotate their password or hit an MFA
re-consent. That is the wrong model for a server.

Missivus does the opposite. An app registration gets the `Mail.Send` **application** permission,
an Exchange application access policy locks it to **one shared mailbox** (no licence needed), and
Odoo authenticates as the app with a client secret. Nothing to click, nothing that expires with a
person. This is the fifth member of the Missivus family — after
[Matomo](https://github.com/Solvetus/missivus-matomo),
[WordPress](https://github.com/Solvetus/missivus-wordpress),
[Nextcloud](https://github.com/Solvetus/missivus-nextcloud) and
[Ghost](https://github.com/Solvetus/missivus-ghost) — and the first written in Python.

| | Delegated OAuth (`microsoft_outlook`) | Missivus |
| --- | --- | --- |
| Who authenticates | A person, in a browser | The application, with a client secret |
| Mail is sent as | That person's mailbox | One shared mailbox, no licence |
| Breaks when | They leave, change password, MFA re-consent | The secret expires (calendar it) |
| Blast radius of the credential | The person's whole mailbox | One mailbox, enforced by Exchange |
| Human interaction | Every (re)connect | None |
| Cost | Free | Free |

## What it does

- Adds **Missivus — Microsoft Graph** as an authentication type on Outgoing Mail Servers.
- Serializes the exact RFC 2822 message Odoo builds — attachments, inline images, headers,
  `Message-Id`, `References` — and posts it as raw MIME to Graph `sendMail`. Nothing is
  re-rendered, so threading and tracking keep working.
- Pins the server's *FROM Filtering* to the shared mailbox, so Odoo's standard routing sends
  matching mail through it and rewrites the From address otherwise. No custom routing code.
- Retries transient failures (429, 5xx, network errors, timeouts) on Odoo's own mail cron: the
  `Retry-After` value when Graph sends one, otherwise 2, 4, 8, 16 and 32 minutes. Five retries,
  then the mail shows *Delivery Failed* with the last Graph error. Permanent failures
  (400, 403, 404) fail immediately with the Graph error code and message.
- *Test Connection* proves the credentials by acquiring a token and shows Microsoft's own error
  description when it cannot.
- Adds **Missivus — Microsoft Graph** as a server type on Incoming Mail Servers: fetches unread
  mail from a shared mailbox and hands it to Odoo's native mail gateway — aliases, catchall,
  threading and bounces work as with IMAP, without IMAP. See [Inbound mail](#inbound-mail).

## What you need

- Odoo 19 (Community or Enterprise), addon path access.
- A Microsoft 365 tenant where you can create an app registration, grant admin consent and run
  Exchange Online PowerShell.
- A shared mailbox to send from (`noreply@example.com` throughout this guide).

## Microsoft side (Azure / Entra)

### Part 1 — Create the app registration

This is the identity Odoo will use. It is not a user and has no password anyone types.

1. Go to <https://entra.microsoft.com> and sign in.
2. In the left-hand menu choose **Applications → App registrations**.
3. Click **+ New registration**.
4. Fill in the form:
   - **Name**: `missivus-odoo-<your Odoo hostname>` — for example `missivus-odoo-erp.example.com`.
     Only administrators see this, and naming it after the tool and the host keeps it straight
     once your tenant holds several registrations.
   - **Supported account types**: **Accounts in this organizational directory only (Single tenant)**
   - **Redirect URI**: leave completely empty. Missivus never redirects a browser anywhere, and an
     empty value here is part of why this setup cannot be hijacked.
5. Click **Register**.
6. On the Overview page copy the **Application (client) ID** and the **Directory (tenant) ID**.
   You will paste both into Odoo.

### Part 2 — Grant the permissions to send and read mail

1. Still inside your app registration, choose **API permissions** in the left-hand menu.
2. Click **+ Add a permission**, then **Microsoft Graph**.
3. Choose **Application permissions**. This is the important choice — *not* "Delegated
   permissions". Application permissions belong to the app itself, which is why no human ever has
   to sign in.
4. In the search box type `Mail.Send`, tick **Mail.Send**; then type `Mail.ReadWrite`, tick
   **Mail.ReadWrite**. Click **Add permissions**.
5. Back on the API permissions page click **✓ Grant admin consent for <your organisation>**,
   then **Yes**. Confirm the Status column now reads **Granted** with a green tick.

> If the "Grant admin consent" button is greyed out, your account cannot consent. Ask a Global
> Administrator to click it. Nothing else in this guide requires their involvement.

> **Why `Mail.ReadWrite` and not `Mail.Read`.** Inbound fetching marks messages as read (or
> moves them) once Odoo has processed them — that is what stops the same message being imported
> twice — and moves messages Odoo cannot process into a quarantine folder. Both are writes. A
> read-only degraded mode is deliberately not offered: without the write, every run would
> re-import the same mail. If you only send, `Mail.Send` alone is still enough.
>
> **The access policy applies to reading too.** The application access policy from Part 5
> restricts *every* Graph mailbox call the app makes, so the mailbox you fetch from must be a
> member of the security group that policy points at — the same group as your sending mailbox,
> or add the fetched mailbox to it. Group names are yours; nothing in this addon depends on them.

You may also see **User.Read** listed as a delegated permission. Azure adds it automatically to
new registrations. Missivus does not use it and you can safely remove it.

### Part 3 — Create a client secret

1. In your app registration choose **Certificates & secrets**.
2. On the **Client secrets** tab click **+ New client secret**.
3. Description `missivus-odoo-<your Odoo hostname>`, pick an expiry, click **Add**.
4. Copy the **Value** column immediately. **It is shown once and never again.** The *Secret ID*
   column is not the secret and is not what you need — this trips almost everyone up the first
   time.
5. Put the expiry date in a calendar. Mail stops on that day if the secret is not replaced.

### Part 4 — Create the shared mailbox

1. Go to <https://admin.exchange.microsoft.com> → **Recipients → Mailboxes → + Add a shared
   mailbox**.
2. Name `No Reply`, address `noreply@example.com` (your own domain). No licence is required.
3. Wait a few minutes: a brand-new mailbox can answer *"mailbox is either inactive, soft-deleted
   or hosted on-premise"* to Graph until provisioning finishes.

### Part 5 — Lock the app to that one mailbox

**Do not skip this.** Until you do, the `Mail.Send` permission you granted in Part 2 lets the app
send as *anyone* in your tenant. An application access policy narrows it to the single shared
mailbox, and it is what makes this whole model safe.

**How it works, in one paragraph.** Exchange cannot point a policy at a single mailbox — it can
only point one at a *group*. So you create a security group whose only member is the shared
mailbox from Part 4, then tell Exchange "this app may only touch mailboxes in that group". The
group gets an email address of its own (`noreply-apps@yourcompany.onmicrosoft.com` below), but
nobody ever sends to it or from it; it exists purely so the policy has something to point at.
Your shared mailbox stays exactly as you created it.

Run these in PowerShell as an Exchange administrator:

```powershell
Install-Module -Name ExchangeOnlineManagement -Scope CurrentUser
Connect-ExchangeOnline -UserPrincipalName admin@yourcompany.onmicrosoft.com
```

```powershell
New-DistributionGroup -Name "NoReply Apps" -Alias noreply-apps -Type Security -Members "noreply@example.com"
```

```powershell
New-ApplicationAccessPolicy -AppId "PASTE-APPLICATION-CLIENT-ID" -PolicyScopeGroupId "noreply-apps@yourcompany.onmicrosoft.com" -AccessRight RestrictAccess -Description "Missivus (Odoo) may only send as noreply@example.com"
```

```powershell
Test-ApplicationAccessPolicy -Identity "noreply@example.com" -AppId "PASTE-APPLICATION-CLIENT-ID"
Test-ApplicationAccessPolicy -Identity "you@example.com" -AppId "PASTE-APPLICATION-CLIENT-ID"
```

The first test must show `AccessCheckResult : Granted`, the second `Denied`. **Do not go further
until it says Denied.** Policies can take up to 30 minutes to become effective; if the second test
still says Granted, wait and run it again. Finish with `Disconnect-ExchangeOnline`.

## Odoo side

1. **Install.** Copy `missivus_mail_graph/` into a folder on your `addons_path`, restart Odoo,
   activate developer mode, **Apps → Update Apps List**, search *Missivus*, **Activate**.
2. **Create the server.** Settings → Technical → Email → **Outgoing Mail Servers** → New:
   - *Name*: `Microsoft Graph`
   - *Authenticate with*: **Missivus — Microsoft Graph**
   - *Directory (tenant) ID*, *Application (client) ID*: from Part 1
   - *Client secret*: the Value from Part 3
   - *Shared mailbox*: `noreply@example.com`
   - *FROM Filtering* fills itself with the shared mailbox on save and stays locked to it.
3. **Test Connection.** The button acquires a token with the saved credentials and reports
   Microsoft's error description when it cannot — `AADSTS7000215` means the Secret *ID* was pasted
   instead of the *Value*, `AADSTS900023` a wrong tenant ID, `AADSTS700016` a wrong client ID.
   **It never sends mail.** Whether the shared mailbox exists and the access policy covers it is
   only proven by a real send (step 5).
4. **Make the shared mailbox Odoo's notification address.** Odoo only routes mail through this
   server when the From address matches the shared mailbox; everything else is rewritten to the
   *default from* of your alias domain first. So set that to the mailbox:
   Settings → General Settings → Discuss → **Alias Domain** (or Technical → Email → **Alias
   Domains**): *Domain* `example.com`, and — with developer mode on — *Default From Alias*
   `noreply` (or the full address `noreply@example.com`). Save. Odoo now sends every notification
   as `"Sender Name via Company" <noreply@example.com>`, which Graph accepts because that is the
   mailbox the app is allowed to use. Mail whose From does not match would otherwise be rejected
   by Graph with `ErrorSendAsDenied`.
5. **Send a real test.** Post a chatter message that notifies a follower with an email address,
   or Settings → Users → your user → *Send an invitation email*. Then check Technical → Email →
   **Emails**: the record should be *Sent*. If it is *Delivery Failed*, the *Failure Reason*
   holds the Graph error code and message verbatim.
6. **Optional — Detect Max Limit.** On the server form, **Detect Max Limit** sets *Convert
   attachments to links for emails over* to 2.5 MB, so heavy attachments become download
   links instead of hitting Graph's 4 MB request cap (Odoo measures the message before the
   extra base64 pass Graph needs, hence the headroom).

## Inbound mail

Odoo's mail gateway turns incoming mail into records and chatter replies: `sales@` creates leads,
`support@` creates tickets, replies land on the right thread, bounces are recognised. Odoo
normally fetches that mail over IMAP. Missivus fetches it over Graph instead, with the same app
registration as the outbound side, and hands each message to the very same Odoo code path.

**What it does, per cron run (every 5 minutes by default):** resolve the folder → list up to
*Messages per run* unread messages → for each: download the raw MIME, give it to Odoo's
`message_process` exactly as the IMAP fetcher would, and — only if Odoo accepted it — mark it as
read or move it. Messages that are still unread at the end of a run are picked up by the next one.

### Set-up

1. Make sure the app has **Mail.ReadWrite** (application) with admin consent — Part 2 — and that
   the mailbox you will fetch from is covered by the access policy — Part 5. Wait for
   `Test-ApplicationAccessPolicy -Identity "inbox@example.com" -AppId "…"` to say **Granted**.
2. Settings → Technical → Email → **Incoming Mail Servers** → New:
   - *Server Type*: **Missivus — Microsoft Graph**
   - *Directory (tenant) ID*, *Application (client) ID*, *Client secret*: as for the outgoing server
   - *Shared mailbox*: the mailbox to fetch from, e.g. `inbox@example.com` (a shared mailbox, no
     licence)
   - *Folder*: `inbox` (default). Well-known names (`inbox`, `archive`, `junkemail`) or the exact
     display name of any folder.
   - *After processing*: **Mark as read** (default) or **Move to folder** with a folder name
     (created if missing).
   - *Messages per run*: 50 by default.
   - *Create a New Record*: optional, like any incoming server — the model to create when no alias
     matches.
3. **Test & Confirm.** Acquires a token with the saved credentials and confirms the server. It
   never reads the mailbox: whether the policy covers this mailbox is proven by the first run.
4. **Fetch Now** on the confirmed server, then check Technical → Email → *Messages* or the target
   model's chatter. The *Advanced* tab shows the last error, if any.

### Post-processing modes

| Mode | After Odoo processed a message | Use when |
| --- | --- | --- |
| Mark as read | The message stays in the folder, read | You want a searchable archive in the mailbox itself |
| Move to folder | The message goes to the named folder (created on first use) | You want the inbox to hold only what Odoo has not seen |

Either way nothing is touched until Odoo has accepted the message, and Odoo ignores a message
whose `Message-Id` it already imported — so a run that is interrupted between processing and
post-processing cannot create duplicates.

### Quarantine

A message Odoo cannot process — malformed MIME, an exception in a `message_new` override, a
record that fails validation — is moved to the **Missivus Quarantine** folder in the same mailbox
(created on first use), *whatever the post-processing mode*, and logged at ERROR level with its
Graph message id and the exception class. The message body is never logged. The run continues
with the remaining messages. If the quarantine move itself fails, the message is left where it is
and retried on the next run.

Transient Graph or network errors (429, 5xx, timeouts) abort the run cleanly: whatever was not
processed stays unread for the next run, and the server's *Last Error* shows the cause. After five
days of continuous failure Odoo sets the server back to *Not Confirmed*, exactly as it does for IMAP.

### Batch cap

*Messages per run* bounds one cron run. A backlog of 10,000 messages drains at 50 per run, i.e.
50 every five minutes with the default cron — raise the cap or the cron frequency for a one-off
migration, then put them back.

## When it does not work

| You see | Usually means |
| --- | --- |
| `AADSTS7000215` | Wrong client secret — the Secret *ID* was copied instead of the *Value* |
| `AADSTS900023` / `AADSTS90002` | Wrong or mistyped Directory (tenant) ID |
| `AADSTS700016` | Wrong Application (client) ID |
| `ErrorAccessDenied` | The access policy does not cover this mailbox, or admin consent was never granted |
| `ErrorSendAsDenied` | The From address is not the shared mailbox — see Odoo side, step 4 |
| `MailboxNotEnabledForRESTAPI` | The sender address is not a real Exchange Online mailbox |
| "mailbox is either inactive, soft-deleted…" | The mailbox has not finished provisioning — wait and retry |
| `message too large for Graph sendMail; reduce attachments` | Over the 4 MB request cap — use **Detect Max Limit** or send fewer/lighter attachments |
| Mail stays *Outgoing* with a future *Scheduled Send Date* | A transient failure (429/5xx/network) is being retried; the *Failure Reason* shows the last error |

Secrets are never part of any error text, so these messages are safe to paste into an issue — but
do read them over first.

## Limitations

- **Outbound only.** Incoming mail, aliases, catchall and bounce handling stay on Odoo's native
  mechanisms (incoming mail servers, mail gateway). Bounces are delivered to the shared mailbox,
  not to Odoo's bounce alias, unless you fetch that mailbox.
- **~4 MB per message** (Graph `sendMail` request cap, base64 included). Upload sessions for
  larger attachments are future work; until then use *Detect Max Limit* so Odoo sends links.
- **Odoo 19 only** (see below).
- **Client secret only.** Certificate credentials are future work.
- One shared mailbox per server record. Add another record for another mailbox.

## Odoo 18 support (not implemented)

The delta is wiring, not logic. Odoo 18 names the hooks `connect()`, `build_email()`,
`_prepare_email_message()` and `_smtp_login()`; Odoo 19 renamed them `_connect__`,
`_build_email__`, `_prepare_email_message__` and `_smtp_login__`, which is what this addon
overrides. `models.Constraint` does not exist in 18 (`_sql_constraints` instead), the form-view
xpaths are the same, and the manifest version becomes `18.0.0.1.0`. `graph_client.py` and the
`mail.mail` retry logic would be unchanged. Untested — contributions welcome.

## Development

Throwaway local environment (never point it at production):

```sh
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml run --rm odoo odoo -d missivus_dev -i missivus_mail_graph --stop-after-init
```

Then open <http://localhost:8069>, database `missivus_dev`, login `admin` / `admin`. The repo
root is mounted as the extra addons path, so edits are live (`--dev=reload,xml`).

Tests run with Odoo's own runner on a fresh database; lint is ruff. CI runs both on every push.

```sh
docker compose -f docker-compose.dev.yml run --rm odoo sh /mnt/extra-addons/tools/run-tests.sh
ruff check . && ruff format --check .
```

The Graph client (`missivus_mail_graph/graph_client.py`) has no Odoo dependency and is tested with
`requests` fully mocked — no test ever talks to Microsoft.

## Security

- The client secret lives in the Outgoing Mail Server record, readable by the *Settings /
  Administration* group only, and is never written to logs or error messages.
- Bearer tokens live in process memory, are refreshed with a 120-second margin and are never
  logged. The raw MIME body is never logged.
- Error text is built from the HTTP status and Microsoft's error code and message only — never
  from request bodies — and is what you see in *Failure Reason* and in *Test Connection*.
- Report vulnerabilities to **security@missivus.com** (see [SECURITY.md](SECURITY.md)).

## Licence

LGPL-3.0-or-later, the Odoo addon convention. Copyright Solvetus.

## Support

Questions and enquiries: **info@missivus.com**. Bugs: GitHub issues. "It doesn't work":
**support@missivus.com**. Solvetus offers paid installation and support — <https://solvetus.com>.

# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Microsoft Graph client for Missivus: client-credentials token, raw-MIME sendMail and the
mailbox read/move calls used for inbound fetching.

Deliberately Odoo-free so it is unit-testable with plain mocks. Error text built here must never
contain the client secret, the bearer token or a MIME body (sent or fetched): it ends up in
mail.mail.failure_reason, in fetchmail.server.error_message, in logs and on screen. Fetched
messages are only ever named by their Graph message id.
"""

import base64
import logging
import re
import threading
import time
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
SENDMAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
GRAPH_BASE = "https://graph.microsoft.com/v1.0/users/{mailbox}"
QUARANTINE_FOLDER = "Missivus Quarantine"
WELL_KNOWN_FOLDERS = frozenset(
    {"inbox", "archive", "drafts", "junkemail", "deleteditems", "sentitems", "outbox"}
)
SCOPE = "https://graph.microsoft.com/.default"
TOKEN_EXPIRY_MARGIN = 120  # seconds subtracted from expires_in before a cached token is reused
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024  # Graph caps the sendMail request body (base64 MIME) at 4 MB
TIMEOUT = (10, 60)  # (connect, read) seconds
# A token that is not header-safe must never reach a request: requests/http.client echo a
# malformed Authorization value verbatim in their exception text.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]+")

# (tenant_id, client_id) -> (access_token, expires_at as time.time())
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_lock = threading.Lock()


class GraphError(Exception):
    """Base class: `status` is the HTTP status, `code` the Entra/Graph error code."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class PermanentDeliveryError(GraphError):
    """Retrying will not help: 400, 403, 404, a second 401, payload too large."""


class TokenError(PermanentDeliveryError):
    """Microsoft Entra refused to issue a token (wrong tenant/client/secret, missing consent)."""


class TransientDeliveryError(GraphError):
    """Retry later: 429, 5xx, network errors, timeouts. `retry_after` in seconds when given."""

    def __init__(self, message, status=None, code=None, retry_after=None):
        super().__init__(message, status=status, code=code)
        self.retry_after = retry_after


class NotFoundError(PermanentDeliveryError):
    """Graph returned 404 for a message: it was deleted or moved between list and fetch."""


class FolderNotFoundError(PermanentDeliveryError):
    """The configured folder does not exist in the mailbox (configuration error)."""


def clear_token_cache():
    with _token_lock:
        _token_cache.clear()


def _retry_after(resp):
    value = resp.headers.get("Retry-After")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _json(resp):
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _post(url, **kwargs):
    try:
        return requests.post(url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        host = url.split("/")[2]
        raise TransientDeliveryError(
            f"Could not reach {host}: {exc.__class__.__name__}: {exc}"
        ) from exc


def get_token(tenant_id, client_id, client_secret, force_refresh=False):
    """Return a bearer token for Graph, from cache while more than TOKEN_EXPIRY_MARGIN remains.

    The lock only guards the cache dict; the HTTP round trip runs outside it so one slow token
    fetch never stalls every other sending thread. A redundant concurrent fetch just stores an
    equally valid token.
    """
    key = (tenant_id, client_id)
    if not force_refresh:
        with _token_lock:
            cached = _token_cache.get(key)
        if cached and cached[1] - TOKEN_EXPIRY_MARGIN > time.time():
            return cached[0]
    resp = _post(
        TOKEN_URL.format(tenant_id=tenant_id),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": SCOPE,
        },
    )
    body = _json(resp)
    if resp.status_code >= 500:
        raise TransientDeliveryError(
            f"Microsoft login returned HTTP {resp.status_code}",
            status=resp.status_code,
            retry_after=_retry_after(resp),
        )
    token = body.get("access_token")
    if resp.status_code != 200 or not token:
        error = body.get("error", "unknown_error")
        description = body.get("error_description") or resp.text[:500]
        raise TokenError(
            f"Token request failed (HTTP {resp.status_code}): {error}: {description}",
            status=resp.status_code,
            code=error,
        )
    if not isinstance(token, str) or not TOKEN_SHAPE.fullmatch(token):
        raise TokenError(
            "Microsoft Entra returned a malformed access token", status=resp.status_code
        )
    with _token_lock:
        _token_cache[key] = (token, time.time() + int(body.get("expires_in", 3600)))
    return token


def _post_sendmail(sender, encoded, token):
    return _post(
        SENDMAIL_URL.format(sender=quote(sender, safe="")),
        data=encoded,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
    )


def _raise_for_graph_status(resp):
    if resp.status_code == 202:
        return
    error = _json(resp).get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("code") or "unknown"
    message = error.get("message") or resp.text[:500] or str(resp.reason)
    text = f"Microsoft Graph sendMail failed (HTTP {resp.status_code} {code}): {message}"
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientDeliveryError(
            text, status=resp.status_code, code=code, retry_after=_retry_after(resp)
        )
    raise PermanentDeliveryError(text, status=resp.status_code, code=code)


def send_raw_mime(sender, raw_bytes, tenant_id, client_id, client_secret):
    """POST an RFC 2822 message as base64 raw MIME to /users/{sender}/sendMail. Returns on 202."""
    encoded = base64.b64encode(raw_bytes)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise PermanentDeliveryError(
            "message too large for Graph sendMail; reduce attachments "
            f"({len(encoded)} bytes encoded, limit {MAX_PAYLOAD_BYTES})"
        )
    token = get_token(tenant_id, client_id, client_secret)
    resp = _post_sendmail(sender, encoded, token)
    if resp.status_code == 401:
        _logger.info("Graph returned 401 for %s; re-acquiring token once", sender)
        token = get_token(tenant_id, client_id, client_secret, force_refresh=True)
        resp = _post_sendmail(sender, encoded, token)
    _raise_for_graph_status(resp)


# ----------------------------------------------------------------------------------------------
# Mailbox read/write (inbound). Same token cache, same 401 refresh-once, same error taxonomy.
# Fetched mail bodies never enter error text or logs: only Graph message ids do.
# ----------------------------------------------------------------------------------------------


def _request(method, url, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    try:
        return requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        host = url.split("/")[2]
        raise TransientDeliveryError(
            f"Could not reach {host}: {exc.__class__.__name__}: {exc}"
        ) from exc


def _graph_call(method, url, creds, *, what, ok=(200, 201, 202, 204), **kwargs):
    """One authenticated Graph call with a single token re-acquire on 401.

    `creds` is (tenant_id, client_id, client_secret); `what` names the operation and the
    message/folder id for error text (never a body).
    """
    token = get_token(*creds)
    resp = _request(method, url, token, **kwargs)
    if resp.status_code == 401:
        _logger.info("Graph returned 401 for %s; re-acquiring token once", what)
        token = get_token(*creds, force_refresh=True)
        resp = _request(method, url, token, **kwargs)
    if resp.status_code in ok:
        return resp
    error = _json(resp).get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("code") or "unknown"
    message = error.get("message") or str(resp.reason)
    text = f"Microsoft Graph {what} failed (HTTP {resp.status_code} {code}): {message}"
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientDeliveryError(
            text, status=resp.status_code, code=code, retry_after=_retry_after(resp)
        )
    if resp.status_code == 404:
        raise NotFoundError(text, status=404, code=code)
    raise PermanentDeliveryError(text, status=resp.status_code, code=code)


def _base(mailbox):
    return GRAPH_BASE.format(mailbox=quote(mailbox, safe=""))


def list_unread_message_ids(mailbox, folder_id, top, tenant_id, client_id, client_secret):
    """Ids of up to `top` unread messages in `folder_id`.

    No $orderby: Graph rejects $filter + $orderby on messages as InefficientFilter, and order is
    irrelevant since every unread message is fetched eventually.
    """
    resp = _graph_call(
        "GET",
        f"{_base(mailbox)}/mailFolders/{quote(folder_id, safe='')}/messages",
        (tenant_id, client_id, client_secret),
        what=f"list unread in folder {folder_id}",
        params={"$filter": "isRead eq false", "$select": "id", "$top": top},
    )
    return [item["id"] for item in _json(resp).get("value", []) if item.get("id")]


def fetch_raw_mime(mailbox, message_id, tenant_id, client_id, client_secret):
    """The RFC 2822 source of one message, as bytes."""
    resp = _graph_call(
        "GET",
        f"{_base(mailbox)}/messages/{quote(message_id, safe='')}/$value",
        (tenant_id, client_id, client_secret),
        what=f"fetch message {message_id}",
    )
    return resp.content


def mark_read(mailbox, message_id, tenant_id, client_id, client_secret):
    _graph_call(
        "PATCH",
        f"{_base(mailbox)}/messages/{quote(message_id, safe='')}",
        (tenant_id, client_id, client_secret),
        what=f"mark read message {message_id}",
        json={"isRead": True},
    )


def move_message(mailbox, message_id, folder_id, tenant_id, client_id, client_secret):
    _graph_call(
        "POST",
        f"{_base(mailbox)}/messages/{quote(message_id, safe='')}/move",
        (tenant_id, client_id, client_secret),
        what=f"move message {message_id}",
        json={"destinationId": folder_id},
    )


def _find_folder_by_name(mailbox, name, creds):
    escaped = name.replace("'", "''")
    resp = _graph_call(
        "GET",
        f"{_base(mailbox)}/mailFolders",
        creds,
        what=f"find folder '{name}'",
        params={"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName"},
    )
    for item in _json(resp).get("value", []):
        if item.get("id"):
            return item["id"]
    return None


def resolve_folder(mailbox, name, tenant_id, client_id, client_secret):
    """Folder id for a well-known name (inbox, archive, ...) or a folder's displayName.

    Not cached across runs: folders can be renamed. Callers resolve once per run.
    """
    creds = (tenant_id, client_id, client_secret)
    key = name.strip()
    if key.lower() in WELL_KNOWN_FOLDERS:
        try:
            resp = _graph_call(
                "GET",
                f"{_base(mailbox)}/mailFolders/{key.lower()}",
                creds,
                what=f"resolve folder '{key}'",
                params={"$select": "id"},
            )
        except NotFoundError as exc:
            raise FolderNotFoundError(str(exc), status=404, code=exc.code) from exc
        return _json(resp)["id"]
    folder_id = _find_folder_by_name(mailbox, key, creds)
    if not folder_id:
        raise FolderNotFoundError(f"Mail folder '{key}' not found in mailbox {mailbox}")
    return folder_id


def ensure_folder(mailbox, name, tenant_id, client_id, client_secret):
    """Folder id by displayName, creating it when missing.

    Safe under two concurrent workers: a create that loses the race (409) re-resolves instead of
    failing.
    """
    creds = (tenant_id, client_id, client_secret)
    folder_id = _find_folder_by_name(mailbox, name, creds)
    if folder_id:
        return folder_id
    try:
        resp = _graph_call(
            "POST",
            f"{_base(mailbox)}/mailFolders",
            creds,
            what=f"create folder '{name}'",
            json={"displayName": name},
        )
    except PermanentDeliveryError as exc:
        if exc.status != 409:
            raise
        folder_id = _find_folder_by_name(mailbox, name, creds)
        if not folder_id:
            raise
        return folder_id
    return _json(resp)["id"]

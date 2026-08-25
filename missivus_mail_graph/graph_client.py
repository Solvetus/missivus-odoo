# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Microsoft Graph client for Missivus: client-credentials token + raw-MIME sendMail.

Deliberately Odoo-free so it is unit-testable with plain mocks. Error text built here must never
contain the client secret, the bearer token or the MIME body: it ends up in
mail.mail.failure_reason and on screen.
"""

import base64
import logging
import threading
import time
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
SENDMAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
SCOPE = "https://graph.microsoft.com/.default"
TOKEN_EXPIRY_MARGIN = 120  # seconds subtracted from expires_in before a cached token is reused
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024  # Graph caps the sendMail request body (base64 MIME) at 4 MB
TIMEOUT = (10, 60)  # (connect, read) seconds

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
    """Return a bearer token for Graph, from cache while more than TOKEN_EXPIRY_MARGIN remains."""
    key = (tenant_id, client_id)
    with _token_lock:
        if not force_refresh:
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

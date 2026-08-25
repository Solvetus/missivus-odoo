# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import json
from unittest.mock import patch

import requests

from .. import graph_client

POST_TARGET = "odoo.addons.missivus_mail_graph.graph_client.requests.post"
REQUEST_TARGET = "odoo.addons.missivus_mail_graph.graph_client.requests.request"

CREDS = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222",
    "client_secret": "s3cr3t-value-never-logged",
}
SENDER = "noreply@example.com"
MAILBOX = "inbox@example.com"
FOLDER_ID = "AAMkAGI2folder="
QUARANTINE_ID = "AAMkAGI2quarantine="


def response(status, body=None, headers=None):
    """Build a real requests.Response so .json()/.text/.headers behave like production."""
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode() if body is not None else b""
    resp.headers.update(headers or {})
    return resp


def token_ok(expires_in=3600, token="tok-1"):
    return response(200, {"access_token": token, "expires_in": expires_in, "token_type": "Bearer"})


def token_error():
    return response(
        401,
        {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided. Ensure the "
            "secret being sent in the request is the client secret value, not the client secret "
            "ID, for a secret added to app '22222222-2222-2222-2222-222222222222'.",
        },
    )


def graph_error(status, code, message, headers=None):
    return response(status, {"error": {"code": code, "message": message}}, headers)


ACCEPTED = response(202)


def graph_json(body, status=200):
    return response(status, body)


def mime_response(raw, status=200):
    resp = requests.Response()
    resp.status_code = status
    resp._content = raw
    resp.headers["Content-Type"] = "message/rfc822"
    return resp


def folder_list(*items):
    """items: (id, displayName) pairs -> a mailFolders collection response."""
    return graph_json({"value": [{"id": i, "displayName": n} for i, n in items]})


def message_list(*ids):
    return graph_json({"value": [{"id": i} for i in ids]})


class GraphPostMock:
    """Context manager: patches requests.post with a scripted list of responses/exceptions.

    Records every call as (url, kwargs) in .calls so tests can assert on headers/body.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self._patcher = patch(POST_TARGET, side_effect=self._post)
        # Mailbox calls (inbound) go through requests.request; token + sendMail keep requests.post
        self._request_patcher = patch(REQUEST_TARGET, side_effect=self._request)

    def _next(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._next()

    def _request(self, method, url, **kwargs):
        self.calls.append((url, dict(kwargs, method=method)))
        return self._next()

    def __enter__(self):
        graph_client.clear_token_cache()
        self._patcher.start()
        self._request_patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        self._request_patcher.stop()
        graph_client.clear_token_cache()
        return False

    @property
    def token_calls(self):
        return [c for c in self.calls if "oauth2/v2.0/token" in c[0]]

    @property
    def send_calls(self):
        return [c for c in self.calls if "/sendMail" in c[0]]

    @property
    def mailbox_calls(self):
        return [c for c in self.calls if "method" in c[1]]


def create_graph_server(env, **vals):
    values = {
        "name": "Missivus Graph",
        "smtp_authentication": "missivus_graph",
        "missivus_tenant_id": CREDS["tenant_id"],
        "missivus_client_id": CREDS["client_id"],
        "missivus_client_secret": CREDS["client_secret"],
        "missivus_sender": SENDER,
    }
    values.update(vals)
    return env["ir.mail_server"].create(values)


def create_graph_fetch_server(env, **vals):
    values = {
        "name": "Missivus Graph inbound",
        "server_type": "missivus_graph",
        "missivus_tenant_id": CREDS["tenant_id"],
        "missivus_client_id": CREDS["client_id"],
        "missivus_client_secret": CREDS["client_secret"],
        "missivus_sender": MAILBOX,
    }
    values.update(vals)
    return env["fetchmail.server"].create(values)

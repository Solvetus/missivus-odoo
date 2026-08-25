# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import json
from unittest.mock import patch

import requests

from .. import graph_client

POST_TARGET = "odoo.addons.missivus_mail_graph.graph_client.requests.post"

CREDS = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222",
    "client_secret": "s3cr3t-value-never-logged",
}
SENDER = "noreply@example.com"


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


class GraphPostMock:
    """Context manager: patches requests.post with a scripted list of responses/exceptions.

    Records every call as (url, kwargs) in .calls so tests can assert on headers/body.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self._patcher = patch(POST_TARGET, side_effect=self._post)

    def _post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        graph_client.clear_token_cache()
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False

    @property
    def token_calls(self):
        return [c for c in self.calls if "oauth2/v2.0/token" in c[0]]

    @property
    def send_calls(self):
        return [c for c in self.calls if "/sendMail" in c[0]]


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

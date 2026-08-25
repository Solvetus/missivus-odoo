# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
from unittest.mock import patch

import requests

from odoo.tests import TransactionCase

from .. import graph_client
from ..graph_client import PermanentDeliveryError, TokenError, TransientDeliveryError
from .common import ACCEPTED, CREDS, SENDER, GraphPostMock, graph_error, token_error, token_ok

RAW = b"From: noreply@example.com\r\nTo: a@example.com\r\nSubject: hi\r\n\r\nbody\r\n"


class TestToken(TransactionCase):
    def test_token_success_is_cached(self):
        with GraphPostMock(token_ok()) as post:
            self.assertEqual(graph_client.get_token(**CREDS), "tok-1")
            self.assertEqual(graph_client.get_token(**CREDS), "tok-1")
            self.assertEqual(len(post.token_calls), 1)
            url, kwargs = post.token_calls[0]
            self.assertEqual(url, graph_client.TOKEN_URL.format(tenant_id=CREDS["tenant_id"]))
            self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
            self.assertEqual(kwargs["data"]["scope"], graph_client.SCOPE)
            self.assertEqual(kwargs["data"]["client_secret"], CREDS["client_secret"])
            self.assertEqual(kwargs["timeout"], graph_client.TIMEOUT)

    def test_token_cache_expiry_margin(self):
        # expires_in=1000 at t=0 -> still valid at t=879, refreshed at t=881 (120 s margin)
        with GraphPostMock(token_ok(1000, "tok-1"), token_ok(1000, "tok-2")) as post:
            with patch.object(graph_client.time, "time", return_value=0):
                graph_client.get_token(**CREDS)
            with patch.object(graph_client.time, "time", return_value=879):
                self.assertEqual(graph_client.get_token(**CREDS), "tok-1")
            with patch.object(graph_client.time, "time", return_value=881):
                self.assertEqual(graph_client.get_token(**CREDS), "tok-2")
            self.assertEqual(len(post.token_calls), 2)

    def test_token_cache_key_is_tenant_and_client(self):
        other = dict(CREDS, client_id="33333333-3333-3333-3333-333333333333")
        with GraphPostMock(token_ok(token="tok-a"), token_ok(token="tok-b")) as post:
            self.assertEqual(graph_client.get_token(**CREDS), "tok-a")
            self.assertEqual(graph_client.get_token(**other), "tok-b")
            self.assertEqual(len(post.token_calls), 2)

    def test_token_force_refresh_bypasses_cache(self):
        with GraphPostMock(token_ok(token="tok-1"), token_ok(token="tok-2")) as post:
            graph_client.get_token(**CREDS)
            self.assertEqual(graph_client.get_token(**CREDS, force_refresh=True), "tok-2")
            self.assertEqual(len(post.token_calls), 2)

    def test_token_failure_carries_aad_description_not_secret(self):
        with GraphPostMock(token_error()):
            with self.assertRaises(TokenError) as cm:
                graph_client.get_token(**CREDS)
        text = str(cm.exception)
        self.assertIn("AADSTS7000215", text)
        self.assertIn("invalid_client", text)
        self.assertEqual(cm.exception.code, "invalid_client")
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn(CREDS["client_secret"], text)
        self.assertIsInstance(cm.exception, PermanentDeliveryError)

    def test_malformed_token_is_rejected_and_never_echoed(self):
        with GraphPostMock(token_ok(token="AAA.SECRET\rX")):
            with self.assertRaises(TokenError) as cm:
                graph_client.get_token(**CREDS)
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertIn("malformed", str(cm.exception))

    def test_token_network_error_is_transient(self):
        with GraphPostMock(requests.ConnectionError("boom")):
            with self.assertRaises(TransientDeliveryError) as cm:
                graph_client.get_token(**CREDS)
        self.assertIn("ConnectionError", str(cm.exception))
        self.assertIsNone(cm.exception.retry_after)

    def test_token_5xx_is_transient(self):
        with GraphPostMock(graph_error(503, "ServiceUnavailable", "try later")):
            with self.assertRaises(TransientDeliveryError):
                graph_client.get_token(**CREDS)


class TestSendRawMime(TransactionCase):
    def _send(self, raw=RAW):
        graph_client.send_raw_mime(SENDER, raw, **CREDS)

    def test_send_success(self):
        with GraphPostMock(token_ok(), ACCEPTED) as post:
            self._send()
            self.assertEqual(len(post.send_calls), 1)
            url, kwargs = post.send_calls[0]
            self.assertEqual(url, graph_client.SENDMAIL_URL.format(sender="noreply%40example.com"))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-1")
            self.assertEqual(kwargs["headers"]["Content-Type"], "text/plain")
            self.assertEqual(kwargs["data"], graph_client.base64.b64encode(RAW))
            self.assertEqual(kwargs["timeout"], graph_client.TIMEOUT)

    def test_401_refreshes_token_once_then_succeeds(self):
        with GraphPostMock(
            token_ok(token="stale"),
            graph_error(401, "InvalidAuthenticationToken", "Access token has expired."),
            token_ok(token="fresh"),
            ACCEPTED,
        ) as post:
            self._send()
            self.assertEqual(len(post.token_calls), 2)
            self.assertEqual(len(post.send_calls), 2)
            self.assertEqual(post.send_calls[1][1]["headers"]["Authorization"], "Bearer fresh")

    def test_second_401_is_permanent(self):
        with GraphPostMock(
            token_ok(),
            graph_error(401, "InvalidAuthenticationToken", "bad"),
            token_ok(),
            graph_error(401, "InvalidAuthenticationToken", "still bad"),
        ) as post:
            with self.assertRaises(PermanentDeliveryError) as cm:
                self._send()
            self.assertEqual(len(post.send_calls), 2)
            self.assertEqual(cm.exception.status, 401)
            self.assertIn("still bad", str(cm.exception))

    def test_permanent_statuses(self):
        cases = (
            (400, "ErrorInvalidRequest"),
            (403, "ErrorAccessDenied"),
            (404, "ErrorInvalidUser"),
        )
        for status, code in cases:
            with (
                self.subTest(status=status),
                GraphPostMock(token_ok(), graph_error(status, code, f"msg {status}")),
            ):
                with self.assertRaises(PermanentDeliveryError) as cm:
                    self._send()
                self.assertEqual(cm.exception.status, status)
                self.assertEqual(cm.exception.code, code)
                self.assertIn(code, str(cm.exception))
                self.assertIn(f"msg {status}", str(cm.exception))
                self.assertNotIsInstance(cm.exception, TransientDeliveryError)

    def test_429_is_transient_with_retry_after(self):
        throttled = graph_error(429, "TooManyRequests", "slow down", {"Retry-After": "120"})
        with GraphPostMock(token_ok(), throttled):
            with self.assertRaises(TransientDeliveryError) as cm:
                self._send()
        self.assertEqual(cm.exception.retry_after, 120)
        self.assertEqual(cm.exception.status, 429)

    def test_5xx_is_transient_without_retry_after(self):
        with GraphPostMock(token_ok(), graph_error(503, "ServiceUnavailable", "later")):
            with self.assertRaises(TransientDeliveryError) as cm:
                self._send()
        self.assertIsNone(cm.exception.retry_after)

    def test_timeout_is_transient(self):
        with GraphPostMock(token_ok(), requests.Timeout("read timed out")):
            with self.assertRaises(TransientDeliveryError):
                self._send()

    def test_oversized_payload_is_permanent_and_never_posted(self):
        big = b"x" * (3 * 1024 * 1024 + 1)  # 3 MiB + 1 -> > 4 MiB once base64-encoded
        with GraphPostMock() as post:
            with self.assertRaises(PermanentDeliveryError) as cm:
                self._send(big)
            self.assertEqual(post.calls, [])
        self.assertIn("message too large for Graph sendMail; reduce attachments", str(cm.exception))

    def test_error_text_never_contains_token(self):
        denied = graph_error(403, "ErrorAccessDenied", "denied")
        with GraphPostMock(token_ok(token="tok-secret"), denied):
            with self.assertRaises(PermanentDeliveryError) as cm:
                self._send()
        self.assertNotIn("tok-secret", str(cm.exception))

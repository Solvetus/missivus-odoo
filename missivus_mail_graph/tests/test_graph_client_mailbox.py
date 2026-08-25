# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
from urllib.parse import quote

import requests

from odoo.tests import TransactionCase

from .. import graph_client
from ..graph_client import (
    FolderNotFoundError,
    NotFoundError,
    PermanentDeliveryError,
    TransientDeliveryError,
)
from .common import (
    CREDS,
    FOLDER_ID,
    MAILBOX,
    GraphPostMock,
    folder_list,
    graph_error,
    graph_json,
    message_list,
    mime_response,
    token_ok,
)

RAW = b"From: a@example.org\r\nTo: inbox@example.com\r\nSubject: hi\r\n\r\nsecret body text\r\n"
BASE = "https://graph.microsoft.com/v1.0/users/inbox%40example.com"


class TestListUnread(TransactionCase):
    def test_query_shape_filter_select_top_no_orderby(self):
        with GraphPostMock(token_ok(), message_list("m1", "m2")) as post:
            ids = graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 50, **CREDS)
        self.assertEqual(ids, ["m1", "m2"])
        url, kwargs = post.mailbox_calls[0]
        self.assertEqual(kwargs["method"], "GET")
        # opaque Graph ids are percent-encoded as one path segment
        self.assertEqual(url, f"{BASE}/mailFolders/{quote(FOLDER_ID, safe='')}/messages")
        self.assertEqual(
            kwargs["params"], {"$filter": "isRead eq false", "$select": "id", "$top": 50}
        )
        self.assertNotIn("$orderby", kwargs["params"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-1")
        self.assertEqual(kwargs["timeout"], graph_client.TIMEOUT)

    def test_empty_folder(self):
        with GraphPostMock(token_ok(), message_list()):
            self.assertEqual(
                graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS), []
            )

    def test_401_refreshes_once(self):
        with GraphPostMock(
            token_ok(token="stale"),
            graph_error(401, "InvalidAuthenticationToken", "expired"),
            token_ok(token="fresh"),
            message_list("m1"),
        ) as post:
            graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS)
        self.assertEqual(post.mailbox_calls[1][1]["headers"]["Authorization"], "Bearer fresh")

    def test_second_401_is_permanent(self):
        with GraphPostMock(
            token_ok(),
            graph_error(401, "InvalidAuthenticationToken", "bad"),
            token_ok(),
            graph_error(401, "InvalidAuthenticationToken", "still bad"),
        ):
            with self.assertRaises(PermanentDeliveryError) as cm:
                graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS)
        self.assertEqual(cm.exception.status, 401)

    def test_transient_and_permanent_mapping(self):
        later = graph_error(503, "ServiceUnavailable", "x", {"Retry-After": "7"})
        with GraphPostMock(token_ok(), later):
            with self.assertRaises(TransientDeliveryError) as cm:
                graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS)
        self.assertEqual(cm.exception.retry_after, 7)
        with GraphPostMock(token_ok(), graph_error(403, "ErrorAccessDenied", "denied")):
            with self.assertRaises(PermanentDeliveryError) as cm:
                graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS)
        self.assertIn("ErrorAccessDenied", str(cm.exception))
        self.assertNotIn(CREDS["client_secret"], str(cm.exception))

    def test_429_is_transient(self):
        throttled = graph_error(429, "TooManyRequests", "slow", {"Retry-After": "120"})
        with GraphPostMock(token_ok(), throttled):
            with self.assertRaises(TransientDeliveryError) as cm:
                graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS)
        self.assertEqual(cm.exception.retry_after, 120)

    def test_network_error_is_transient(self):
        with GraphPostMock(token_ok(), requests.ConnectionError("boom")):
            with self.assertRaises(TransientDeliveryError):
                graph_client.list_unread_message_ids(MAILBOX, FOLDER_ID, 5, **CREDS)


class TestFetchRawMime(TransactionCase):
    def test_returns_exact_bytes(self):
        with GraphPostMock(token_ok(), mime_response(RAW)) as post:
            raw = graph_client.fetch_raw_mime(MAILBOX, "m1", **CREDS)
        self.assertEqual(raw, RAW)
        url, kwargs = post.mailbox_calls[0]
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(url, f"{BASE}/messages/m1/$value")

    def test_404_is_not_found_and_error_text_has_no_body(self):
        with GraphPostMock(token_ok(), graph_error(404, "ErrorItemNotFound", "gone")):
            with self.assertRaises(NotFoundError) as cm:
                graph_client.fetch_raw_mime(MAILBOX, "m1", **CREDS)
        self.assertIsInstance(cm.exception, PermanentDeliveryError)
        self.assertIn("m1", str(cm.exception))
        self.assertNotIn("secret body text", str(cm.exception))


class TestMarkReadAndMove(TransactionCase):
    def test_mark_read_patch_body(self):
        with GraphPostMock(token_ok(), graph_json({"id": "m1", "isRead": True})) as post:
            graph_client.mark_read(MAILBOX, "m1", **CREDS)
        url, kwargs = post.mailbox_calls[0]
        self.assertEqual(kwargs["method"], "PATCH")
        self.assertEqual(url, f"{BASE}/messages/m1")
        self.assertEqual(kwargs["json"], {"isRead": True})

    def test_move_post_body(self):
        with GraphPostMock(token_ok(), graph_json({"id": "m1-new"}, status=201)) as post:
            graph_client.move_message(MAILBOX, "m1", FOLDER_ID, **CREDS)
        url, kwargs = post.mailbox_calls[0]
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(url, f"{BASE}/messages/m1/move")
        self.assertEqual(kwargs["json"], {"destinationId": FOLDER_ID})

    def test_move_404_is_not_found(self):
        with GraphPostMock(token_ok(), graph_error(404, "ErrorItemNotFound", "gone")):
            with self.assertRaises(NotFoundError):
                graph_client.move_message(MAILBOX, "m1", FOLDER_ID, **CREDS)


class TestFolders(TransactionCase):
    def test_resolve_well_known_inbox(self):
        inbox = graph_json({"id": FOLDER_ID, "displayName": "Inbox"})
        with GraphPostMock(token_ok(), inbox) as post:
            self.assertEqual(graph_client.resolve_folder(MAILBOX, "inbox", **CREDS), FOLDER_ID)
        url, kwargs = post.mailbox_calls[0]
        self.assertEqual(url, f"{BASE}/mailFolders/inbox")
        self.assertEqual(kwargs["params"], {"$select": "id"})

    def test_resolve_well_known_is_case_insensitive(self):
        with GraphPostMock(token_ok(), graph_json({"id": FOLDER_ID})) as post:
            graph_client.resolve_folder(MAILBOX, "  Inbox ", **CREDS)
        self.assertEqual(post.mailbox_calls[0][0], f"{BASE}/mailFolders/inbox")

    def test_resolve_custom_by_display_name(self):
        with GraphPostMock(token_ok(), folder_list(("f9", "Odoo Leads"))) as post:
            self.assertEqual(graph_client.resolve_folder(MAILBOX, "Odoo Leads", **CREDS), "f9")
        url, kwargs = post.mailbox_calls[0]
        self.assertEqual(url, f"{BASE}/mailFolders")
        self.assertEqual(
            kwargs["params"],
            {"$filter": "displayName eq 'Odoo Leads'", "$select": "id,displayName"},
        )

    def test_resolve_escapes_single_quote(self):
        with GraphPostMock(token_ok(), folder_list(("f1", "Bob's"))) as post:
            graph_client.resolve_folder(MAILBOX, "Bob's", **CREDS)
        self.assertEqual(post.mailbox_calls[0][1]["params"]["$filter"], "displayName eq 'Bob''s'")

    def test_resolve_custom_not_found(self):
        with GraphPostMock(token_ok(), folder_list()):
            with self.assertRaises(FolderNotFoundError) as cm:
                graph_client.resolve_folder(MAILBOX, "Nope", **CREDS)
        self.assertIn("Nope", str(cm.exception))
        self.assertIsInstance(cm.exception, PermanentDeliveryError)

    def test_resolve_well_known_404_is_folder_not_found(self):
        with GraphPostMock(token_ok(), graph_error(404, "ErrorFolderNotFound", "no such")):
            with self.assertRaises(FolderNotFoundError):
                graph_client.resolve_folder(MAILBOX, "archive", **CREDS)

    def test_ensure_folder_existing(self):
        with GraphPostMock(token_ok(), folder_list(("q1", "Missivus Quarantine"))) as post:
            self.assertEqual(
                graph_client.ensure_folder(MAILBOX, "Missivus Quarantine", **CREDS), "q1"
            )
        self.assertEqual(len(post.mailbox_calls), 1)

    def test_ensure_folder_creates(self):
        created = graph_json({"id": "q2"}, status=201)
        with GraphPostMock(token_ok(), folder_list(), created) as post:
            self.assertEqual(
                graph_client.ensure_folder(MAILBOX, "Missivus Quarantine", **CREDS), "q2"
            )
        url, kwargs = post.mailbox_calls[1]
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(url, f"{BASE}/mailFolders")
        self.assertEqual(kwargs["json"], {"displayName": "Missivus Quarantine"})

    def test_ensure_folder_race_re_resolves(self):
        # Two workers: the other one created it between our list and our POST (409)
        with GraphPostMock(
            token_ok(),
            folder_list(),
            graph_error(409, "ErrorFolderExists", "exists"),
            folder_list(("q3", "Missivus Quarantine")),
        ):
            self.assertEqual(
                graph_client.ensure_folder(MAILBOX, "Missivus Quarantine", **CREDS), "q3"
            )

    def test_ensure_folder_race_lost_and_still_missing_raises(self):
        with GraphPostMock(
            token_ok(),
            folder_list(),
            graph_error(409, "ErrorFolderExists", "exists"),
            folder_list(),
        ):
            with self.assertRaises(PermanentDeliveryError) as cm:
                graph_client.ensure_folder(MAILBOX, "Missivus Quarantine", **CREDS)
        self.assertEqual(cm.exception.status, 409)

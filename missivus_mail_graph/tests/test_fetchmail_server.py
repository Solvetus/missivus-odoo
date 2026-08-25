# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import logging
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase
from odoo.tools import mute_logger

from ..graph_client import TransientDeliveryError
from .common import (
    CREDS,
    FOLDER_ID,
    QUARANTINE_ID,
    GraphPostMock,
    create_graph_fetch_server,
    folder_list,
    graph_error,
    graph_json,
    message_list,
    mime_response,
    token_error,
    token_ok,
)

LOOP_LOGGER = "odoo.addons.missivus_mail_graph.models.fetchmail_server"
RAW1 = (
    b"From: a@example.org\r\nTo: inbox@example.com\r\nSubject: one\r\nMessage-Id: <1@x>\r\n"
    b"\r\nbody one SECRET-BODY-1\r\n"
)
RAW2 = (
    b"From: b@example.org\r\nTo: inbox@example.com\r\nSubject: two\r\nMessage-Id: <2@x>\r\n"
    b"\r\nbody two SECRET-BODY-2\r\n"
)


class TestFetchServerConfig(TransactionCase):
    def test_selection_value_exists(self):
        selection = dict(self.env["fetchmail.server"]._fields["server_type"].selection)
        self.assertEqual(selection["missivus_graph"], "Missivus — Microsoft Graph")

    def test_defaults(self):
        server = create_graph_fetch_server(self.env)
        self.assertEqual(server.missivus_folder, "inbox")
        self.assertEqual(server.missivus_post_process, "mark_read")
        self.assertEqual(server.missivus_batch_cap, 50)
        self.assertEqual(server.state, "draft")
        self.assertIn("Microsoft Graph", server.server_type_info)

    def test_graph_fields_required(self):
        with self.assertRaises(ValidationError):
            create_graph_fetch_server(self.env, missivus_client_secret=False)

    def test_mailbox_must_be_email(self):
        with self.assertRaises(ValidationError):
            create_graph_fetch_server(self.env, missivus_sender="inbox")

    def test_batch_cap_positive(self):
        for bad in (0, -1):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                create_graph_fetch_server(self.env, missivus_batch_cap=bad)

    def test_move_mode_requires_target_folder(self):
        with self.assertRaises(ValidationError):
            create_graph_fetch_server(self.env, missivus_post_process="move")
        server = create_graph_fetch_server(
            self.env, missivus_post_process="move", missivus_move_folder="Processed"
        )
        self.assertEqual(server.missivus_move_folder, "Processed")

    def test_folder_required(self):
        with self.assertRaises(ValidationError):
            create_graph_fetch_server(self.env, missivus_folder=" ")

    def test_imap_server_untouched_by_constraints(self):
        # no Graph fields, zero batch cap: the constraints only apply to our type
        server = self.env["fetchmail.server"].create(
            {"name": "imap", "server_type": "imap", "missivus_batch_cap": 0}
        )
        self.assertEqual(server.missivus_folder, "inbox")  # plain field default
        self.assertFalse(server.server_type_info)


class TestConfirmButton(TransactionCase):
    def setUp(self):
        super().setUp()
        self.server = create_graph_fetch_server(self.env)

    def test_success_acquires_token_only_and_confirms(self):
        with GraphPostMock(token_ok()) as post:
            self.server.button_confirm_login()
        self.assertEqual(len(post.token_calls), 1)
        self.assertEqual(post.mailbox_calls, [])
        self.assertEqual(self.server.state, "done")

    def test_failure_reports_aad_description_and_stays_draft(self):
        with GraphPostMock(token_error()):
            with self.assertRaises(UserError) as cm:
                self.server.button_confirm_login()
        self.assertIn("AADSTS7000215", str(cm.exception))
        self.assertNotIn(self.server.missivus_client_secret, str(cm.exception))
        self.assertEqual(self.server.state, "draft")

    def test_confirm_activates_native_fetchmail_cron(self):
        cron = self.env.ref("mail.ir_cron_mail_gateway_action")
        self.env["fetchmail.server"].search([("id", "!=", self.server.id)]).action_archive()
        with GraphPostMock(token_ok()):
            self.server.button_confirm_login()
        self.assertTrue(cron.active)


class TestFetchLoop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["fetchmail.server"].search([]).action_archive()
        self.server = create_graph_fetch_server(self.env, state="done")
        self.processed = []

        def message_process(obj, model, message, **kw):
            self.processed.append((model, message, kw))
            return 1

        patcher = patch.object(
            self.registry["mail.thread"],
            "message_process",
            side_effect=message_process,
            autospec=True,
        )
        self.process = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _inbox():
        return graph_json({"id": FOLDER_ID, "displayName": "Inbox"})

    @staticmethod
    def _ok():
        return graph_json({"id": "x"})

    def _run(self, *script, batch_limit=50):
        with (
            self.enter_registry_test_mode(),
            self.registry.cursor() as cr,
            GraphPostMock(*script) as post,
        ):
            server = self.server.with_env(self.server.env(cr=cr))
            exc = server._fetch_mail(batch_limit=batch_limit)
            server.invalidate_recordset()
            state = {"error_message": server.error_message, "date": server.date}
        return exc, post, state

    @staticmethod
    def _calls(post, method, fragment):
        return [c for c in post.mailbox_calls if c[1]["method"] == method and fragment in c[0]]

    def test_happy_fetch_marks_read_with_exact_bytes(self):
        exc, post, state = self._run(
            token_ok(), self._inbox(), message_list("m1"), mime_response(RAW1), self._ok()
        )
        self.assertIsNone(exc)
        self.assertEqual(len(self.processed), 1)
        model, message, kw = self.processed[0]
        self.assertEqual(message, RAW1)
        self.assertFalse(model)
        self.assertFalse(kw["save_original"])
        self.assertFalse(kw["strip_attachments"])
        patches = self._calls(post, "PATCH", "/messages/m1")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][1]["json"], {"isRead": True})
        self.assertFalse(state["error_message"])
        self.assertTrue(state["date"])

    def test_native_context_and_object_id_forwarded(self):
        model = self.env["ir.model"]._get("res.partner")
        self.server.write({"object_id": model.id, "original": True, "attach": False})
        self._run(token_ok(), self._inbox(), message_list("m1"), mime_response(RAW1), self._ok())
        model_name, _message, kw = self.processed[0]
        self.assertEqual(model_name, "res.partner")
        self.assertTrue(kw["save_original"])
        self.assertTrue(kw["strip_attachments"])
        obj = self.process.call_args.args[0]
        self.assertEqual(obj.env.context.get("default_fetchmail_server_id"), self.server.id)

    def test_move_mode(self):
        self.server.write({"missivus_post_process": "move", "missivus_move_folder": "Processed"})
        exc, post, _ = self._run(
            token_ok(),
            self._inbox(),
            folder_list(("p1", "Processed")),
            message_list("m1"),
            mime_response(RAW1),
            graph_json({"id": "m1-moved"}, status=201),
        )
        self.assertIsNone(exc)
        moves = self._calls(post, "POST", "/messages/m1/move")
        self.assertEqual(moves[0][1]["json"], {"destinationId": "p1"})
        self.assertEqual(self._calls(post, "PATCH", "/messages"), [])

    def test_custom_source_folder(self):
        self.server.missivus_folder = "Odoo Leads"
        _, post, _ = self._run(token_ok(), folder_list(("f9", "Odoo Leads")), message_list())
        self.assertEqual(len(self._calls(post, "GET", "/mailFolders/f9/messages")), 1)

    @mute_logger(LOOP_LOGGER)
    def test_source_folder_not_found_is_clear_config_error(self):
        self.server.missivus_folder = "Nope"
        exc, _post, state = self._run(token_ok(), folder_list())
        self.assertIsNotNone(exc)
        self.assertIn("Nope", state["error_message"])
        self.assertEqual(self.processed, [])

    def test_batch_cap_and_list_shape(self):
        self.server.missivus_batch_cap = 2
        _, post, _ = self._run(token_ok(), self._inbox(), message_list())
        params = self._calls(post, "GET", "/messages")[0][1]["params"]
        self.assertEqual(params, {"$filter": "isRead eq false", "$select": "id", "$top": 2})

    def test_native_batch_limit_caps_too(self):
        self.server.missivus_batch_cap = 50
        _, post, _ = self._run(token_ok(), self._inbox(), message_list(), batch_limit=3)
        self.assertEqual(self._calls(post, "GET", "/messages")[0][1]["params"]["$top"], 3)

    def test_empty_folder(self):
        exc, _post, state = self._run(token_ok(), self._inbox(), message_list())
        self.assertIsNone(exc)
        self.assertEqual(self.processed, [])
        self.assertFalse(state["error_message"])

    @mute_logger(LOOP_LOGGER)
    def test_transient_error_aborts_run_nothing_post_processed(self):
        exc, post, state = self._run(
            token_ok(),
            self._inbox(),
            message_list("m1", "m2"),
            mime_response(RAW1),
            self._ok(),
            graph_error(503, "ServiceUnavailable", "later"),
        )
        self.assertIsInstance(exc, TransientDeliveryError)
        self.assertEqual(len(self.processed), 1)  # m1 done, m2 fetch failed -> run aborted
        self.assertEqual(len(self._calls(post, "PATCH", "/messages")), 1)
        self.assertIn("ServiceUnavailable", state["error_message"])

    def test_permanent_processing_failure_quarantines_and_continues(self):
        self.process.side_effect = [ValueError("boom"), 1]
        with self.assertLogs(LOOP_LOGGER, level="ERROR") as logs:
            exc, post, _ = self._run(
                token_ok(),
                self._inbox(),
                message_list("m1", "m2"),
                mime_response(RAW1),
                folder_list(),  # quarantine lookup
                graph_json({"id": QUARANTINE_ID}, status=201),  # quarantine created
                graph_json({"id": "m1-q"}, status=201),  # m1 moved
                mime_response(RAW2),
                self._ok(),  # m2 marked read
            )
        self.assertIsNone(exc)
        moves = self._calls(post, "POST", "/messages/m1/move")
        self.assertEqual(moves[0][1]["json"], {"destinationId": QUARANTINE_ID})
        self.assertEqual(len(self._calls(post, "PATCH", "/messages/m2")), 1)
        self.assertEqual(self._calls(post, "PATCH", "/messages/m1"), [])
        text = "\n".join(logs.output)
        self.assertIn("m1", text)
        self.assertIn("ValueError", text)
        self.assertNotIn("SECRET-BODY-1", text)
        self.assertNotIn(CREDS["client_secret"], text)

    def test_quarantine_created_once_per_run(self):
        self.process.side_effect = [ValueError("a"), ValueError("b")]
        with mute_logger(LOOP_LOGGER):
            _, post, _ = self._run(
                token_ok(),
                self._inbox(),
                message_list("m1", "m2"),
                mime_response(RAW1),
                folder_list(),
                graph_json({"id": QUARANTINE_ID}, status=201),
                graph_json({"id": "q"}, status=201),
                mime_response(RAW2),
                graph_json({"id": "q"}, status=201),
            )
        self.assertEqual(len(self._calls(post, "POST", "/mailFolders")), 1)

    def test_quarantine_move_failure_leaves_message_and_continues(self):
        self.process.side_effect = [ValueError("boom"), 1]
        with self.assertLogs(LOOP_LOGGER, level="ERROR") as logs:
            exc, post, _ = self._run(
                token_ok(),
                self._inbox(),
                message_list("m1", "m2"),
                mime_response(RAW1),
                folder_list(("q1", "Missivus Quarantine")),
                graph_error(403, "ErrorAccessDenied", "denied"),  # move fails
                mime_response(RAW2),
                self._ok(),
            )
        self.assertIsNone(exc)
        self.assertEqual(self._calls(post, "PATCH", "/messages/m1"), [])
        self.assertEqual(len(self._calls(post, "PATCH", "/messages/m2")), 1)
        self.assertIn("ErrorAccessDenied", "\n".join(logs.output))

    def test_404_between_list_and_fetch_is_skipped(self):
        # numeric level: inside Odoo the name "INFO" resolves to 25 (Odoo's TEST level)
        with self.assertLogs(LOOP_LOGGER, level=logging.INFO) as logs:
            exc, _post, _ = self._run(
                token_ok(),
                self._inbox(),
                message_list("m1", "m2"),
                graph_error(404, "ErrorItemNotFound", "gone"),
                mime_response(RAW2),
                self._ok(),
            )
        self.assertIsNone(exc)
        self.assertEqual(len(self.processed), 1)
        self.assertEqual(self.processed[0][1], RAW2)
        self.assertIn("m1", "\n".join(logs.output))

    def test_malformed_mime_does_not_kill_run(self):
        self.process.side_effect = [RuntimeError("parse failed"), 1]
        with mute_logger(LOOP_LOGGER):
            exc, _post, _ = self._run(
                token_ok(),
                self._inbox(),
                message_list("m1", "m2"),
                mime_response(b"\xff\xfe not mime at all"),
                folder_list(("q1", "Missivus Quarantine")),
                graph_json({"id": "q"}, status=201),
                mime_response(RAW2),
                self._ok(),
            )
        self.assertIsNone(exc)
        self.assertEqual(self.process.call_count, 2)

    def test_imap_server_still_uses_native_loop(self):
        imap = self.env["fetchmail.server"].create(
            {"name": "imap", "server_type": "imap", "state": "done"}
        )
        with (
            patch.object(
                self.registry["fetchmail.server"], "_connect__", side_effect=OSError("no net")
            ),
            mute_logger("odoo.addons.mail.models.fetchmail"),
            self.enter_registry_test_mode(),
            self.registry.cursor() as cr,
            GraphPostMock() as post,
        ):
            exc = imap.with_env(imap.env(cr=cr))._fetch_mail()
        self.assertIsInstance(exc, OSError)
        self.assertEqual(post.calls, [])

    def test_no_body_secret_or_token_in_any_log(self):
        # Every failure branch in one run: processing failure + quarantine create, quarantine
        # move failure, 404 between list and fetch, permanent post-process failure.
        self.process.side_effect = [ValueError("boom"), 1]
        with self.assertLogs("odoo.addons.missivus_mail_graph", level=logging.DEBUG) as logs:
            _, _post, state = self._run(
                token_ok(token="tok-1"),
                self._inbox(),
                message_list("m1", "m2", "m3"),
                mime_response(RAW1),
                folder_list(),
                graph_json({"id": QUARANTINE_ID}, status=201),
                graph_error(403, "ErrorAccessDenied", "denied"),
                graph_error(404, "ErrorItemNotFound", "gone"),
                mime_response(RAW2),
                graph_error(400, "ErrorInvalidRequest", "bad patch"),
            )
        text = "\n".join(logs.output) + (state["error_message"] or "")
        self.assertIn("m1", text)  # the guard must have seen the failure branches
        for forbidden in ("SECRET-BODY-1", "SECRET-BODY-2", CREDS["client_secret"], "tok-1"):
            self.assertNotIn(forbidden, text)

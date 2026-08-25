# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase

from .common import GraphPostMock, create_graph_fetch_server, token_error, token_ok


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

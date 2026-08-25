# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase

from .common import SENDER, create_graph_server


class TestMailServerConfig(TransactionCase):
    def test_selection_value_exists(self):
        selection = dict(self.env["ir.mail_server"]._fields["smtp_authentication"].selection)
        self.assertEqual(selection["missivus_graph"], "Missivus — Microsoft Graph")

    def test_from_filter_set_on_create(self):
        server = create_graph_server(self.env, from_filter="something-else.example")
        self.assertEqual(server.from_filter, SENDER)

    def test_from_filter_follows_sender_on_write(self):
        server = create_graph_server(self.env)
        server.write({"missivus_sender": "Alerts@Example.com"})
        self.assertEqual(server.from_filter, "Alerts@Example.com")
        # user edits are overridden while Graph is selected
        server.write({"from_filter": "example.com"})
        self.assertEqual(server.from_filter, "Alerts@Example.com")

    def test_switching_to_graph_sets_from_filter(self):
        server = self.env["ir.mail_server"].create(
            {"name": "smtp", "smtp_host": "mail.example.com"}
        )
        server.write(
            {
                "smtp_authentication": "missivus_graph",
                "missivus_tenant_id": "t",
                "missivus_client_id": "c",
                "missivus_client_secret": "s",
                "missivus_sender": SENDER,
            }
        )
        self.assertEqual(server.from_filter, SENDER)

    def test_smtp_server_from_filter_untouched(self):
        server = self.env["ir.mail_server"].create(
            {"name": "smtp", "smtp_host": "mail.example.com", "from_filter": "example.com"}
        )
        self.assertEqual(server.from_filter, "example.com")

    def test_sender_must_be_plausible_email(self):
        for bad in ("noreply", "noreply@", "@example.com", "no reply@example.com"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                create_graph_server(self.env, missivus_sender=bad)

    def test_graph_fields_required(self):
        with self.assertRaises(ValidationError):
            create_graph_server(self.env, missivus_client_secret=False)

    def test_authentication_info_text(self):
        server = create_graph_server(self.env)
        self.assertIn("Microsoft Graph", server.smtp_authentication_info)

    def test_test_email_from_is_sender(self):
        self.assertEqual(create_graph_server(self.env)._get_test_email_from(), SENDER)

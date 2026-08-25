# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import base64
import email.policy
from email.parser import BytesParser
from unittest.mock import patch

from odoo.addons.base.models.ir_mail_server import MailDeliveryException
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase

from ..graph_client import TransientDeliveryError
from ..models.ir_mail_server import MissivusGraphSession, pending_transient
from .common import ACCEPTED, SENDER, GraphPostMock, create_graph_server, graph_error, token_ok


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


# ----------------------------------------------------------------------------------------------
# Send path
# ----------------------------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.4 fake report"


class TestGraphSend(TransactionCase):
    def setUp(self):
        super().setUp()
        self.server = create_graph_server(self.env)
        self.IrMailServer = self.env["ir.mail_server"]
        # Odoo short-circuits sending in tests; we want the real Graph path with requests mocked.
        patcher = patch.object(type(self.IrMailServer), "_disable_send", lambda _: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _message(self, **kwargs):
        return self.IrMailServer._build_email__(
            email_from=kwargs.pop("email_from", '"Odoo" <noreply@example.com>'),
            email_to=kwargs.pop("email_to", ["Alice <alice@example.org>"]),
            subject=kwargs.pop("subject", "Invoice INV/2026/001"),
            body=kwargs.pop("body", '<p>Hello <img src="cid:logo123"/></p>'),
            subtype="html",
            attachments=kwargs.pop("attachments", [("report.pdf", PDF, "application/pdf")]),
            **kwargs,
        )

    def test_connect_returns_graph_session(self):
        session = self.IrMailServer._connect__(mail_server_id=self.server.id, smtp_from=SENDER)
        self.assertIsInstance(session, MissivusGraphSession)
        self.assertEqual(session.from_filter, SENDER)
        self.assertEqual(session.smtp_from, SENDER)
        self.assertEqual(session.server, self.server)
        session.quit()  # must not raise

    def test_connect_resolves_default_graph_server(self):
        session = self.IrMailServer._connect__(smtp_from=SENDER)
        self.assertIsInstance(session, MissivusGraphSession)

    def test_send_email_posts_raw_mime_with_attachment_and_cid(self):
        msg = self._message()
        msg.add_attachment(
            PNG,
            maintype="image",
            subtype="png",
            filename="logo.png",
            cid="<logo123>",
            disposition="inline",
        )
        msg["Bcc"] = "hidden@example.net"
        with GraphPostMock(token_ok(), ACCEPTED) as post:
            message_id = self.IrMailServer.send_email(msg, mail_server_id=self.server.id)
        self.assertEqual(message_id, msg["Message-Id"])
        url, kwargs = post.send_calls[0]
        self.assertIn("noreply%40example.com/sendMail", url)
        self.assertEqual(kwargs["headers"]["Content-Type"], "text/plain")
        raw = base64.b64decode(kwargs["data"])
        self.assertIn(b"\r\n", raw)  # RFC 2822 line endings
        parsed = BytesParser(policy=email.policy.default).parsebytes(raw)
        self.assertEqual(parsed["Subject"], "Invoice INV/2026/001")
        self.assertEqual(parsed["Message-Id"], msg["Message-Id"])
        self.assertIn("alice@example.org", parsed["To"])
        self.assertEqual(parsed["Bcc"], "hidden@example.net")  # envelope-only recipient restored
        parts = {p.get_filename(): p for p in parsed.walk() if p.get_filename()}
        self.assertEqual(parts["report.pdf"].get_payload(decode=True), PDF)
        self.assertEqual(parts["logo.png"]["Content-ID"], "<logo123>")
        self.assertEqual(parts["logo.png"].get_payload(decode=True), PNG)
        self.assertIn("cid:logo123", parsed.get_body(("html",)).get_content())

    def test_send_email_rewrites_from_to_sender_via_odoo_routing(self):
        # No mail_server_id: Odoo's own _find_mail_server picks the Graph server through
        # from_filter == sender and, because the notification address is the shared mailbox,
        # encapsulates the foreign From ("Someone via ..." <noreply@example.com>). No custom code.
        msg = self._message(email_from='"Someone" <someone@other.example>')
        with GraphPostMock(token_ok(), ACCEPTED) as post:
            self.IrMailServer.with_context(domain_notifications_email=SENDER).send_email(msg)
        raw = base64.b64decode(post.send_calls[0][1]["data"])
        parsed = BytesParser(policy=email.policy.default).parsebytes(raw)
        self.assertIn(SENDER, parsed["From"])
        self.assertNotIn("someone@other.example", parsed["From"])

    def test_permanent_error_raises_mail_delivery_exception_with_graph_message(self):
        denied = graph_error(
            403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."
        )
        with GraphPostMock(token_ok(), denied):
            with self.assertRaises(MailDeliveryException) as cm:
                self.IrMailServer.send_email(self._message(), mail_server_id=self.server.id)
        text = str(cm.exception)
        self.assertIn("ErrorAccessDenied", text)
        self.assertIn("Access is denied. Check credentials and try again.", text)
        self.assertIn("HTTP 403", text)
        self.assertNotIn(self.server.missivus_client_secret, text)

    def test_transient_error_propagates_and_is_parked_for_mail_mail(self):
        msg = self._message()
        later = graph_error(503, "ServiceUnavailable", "later", {"Retry-After": "30"})
        with GraphPostMock(token_ok(), later):
            with self.assertRaises(TransientDeliveryError):
                self.IrMailServer.send_email(msg, mail_server_id=self.server.id)
        parked = pending_transient.error
        self.assertEqual(parked.message_id, msg["Message-Id"])
        self.assertEqual(parked.retry_after, 30)
        pending_transient.error = None

    def test_oversized_message_is_permanent(self):
        big = [("big.bin", b"x" * (3 * 1024 * 1024 + 1), "application/octet-stream")]
        with GraphPostMock(token_ok()) as post, self.assertRaises(MailDeliveryException) as cm:
            self.IrMailServer.send_email(
                self._message(attachments=big), mail_server_id=self.server.id
            )
        self.assertIn("message too large for Graph sendMail; reduce attachments", str(cm.exception))
        self.assertEqual(post.send_calls, [])

    def test_smtp_server_path_untouched(self):
        smtp = self.env["ir.mail_server"].create({"name": "smtp", "smtp_host": "mail.example.com"})
        with patch("smtplib.SMTP", side_effect=OSError("no network in tests")):
            with self.assertRaises(OSError):
                self.IrMailServer._connect__(mail_server_id=smtp.id, smtp_from="x@example.com")

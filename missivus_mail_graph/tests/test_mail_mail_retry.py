# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import smtplib
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase
from odoo.tools import mute_logger

from .common import ACCEPTED, GraphPostMock, create_graph_server, graph_error, token_ok

MAIL_LOGGER = "odoo.addons.mail.models.mail_mail"


class TestTransientRetry(TransactionCase):
    def setUp(self):
        super().setUp()
        self.server = create_graph_server(self.env)
        patcher = patch.object(type(self.env["ir.mail_server"]), "_disable_send", lambda _: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mail(self, **vals):
        values = {
            "subject": "Queued",
            "body_html": "<p>queued</p>",
            "email_from": "noreply@example.com",
            "email_to": "bob@example.org",
            "mail_server_id": self.server.id,
        }
        values.update(vals)
        return self.env["mail.mail"].create(values)

    def _assert_scheduled_in(self, mail, seconds):
        expected = fields.Datetime.now() + timedelta(seconds=seconds)
        self.assertAlmostEqual(mail.scheduled_date, expected, delta=timedelta(seconds=5))

    @mute_logger(MAIL_LOGGER)
    def test_transient_error_requeues_with_first_backoff(self):
        mail = self._mail()
        with GraphPostMock(token_ok(), graph_error(503, "ServiceUnavailable", "later")):
            mail.send()
        self.assertEqual(mail.state, "outgoing")
        self.assertEqual(mail.missivus_retry_count, 1)
        self._assert_scheduled_in(mail, 2 * 60)
        self.assertIn("ServiceUnavailable", mail.failure_reason)

    @mute_logger(MAIL_LOGGER)
    def test_backoff_progression(self):
        for count, minutes in ((1, 4), (2, 8), (3, 16), (4, 32)):
            with self.subTest(count=count):
                mail = self._mail(missivus_retry_count=count)
                with GraphPostMock(token_ok(), graph_error(500, "InternalServerError", "x")):
                    mail.send()
                self.assertEqual(mail.state, "outgoing")
                self.assertEqual(mail.missivus_retry_count, count + 1)
                self._assert_scheduled_in(mail, minutes * 60)

    @mute_logger(MAIL_LOGGER)
    def test_retry_after_header_wins(self):
        mail = self._mail()
        throttled = graph_error(429, "TooManyRequests", "slow", {"Retry-After": "600"})
        with GraphPostMock(token_ok(), throttled):
            mail.send()
        self.assertEqual(mail.state, "outgoing")
        self._assert_scheduled_in(mail, 600)

    @mute_logger(MAIL_LOGGER)
    def test_retry_cap_lands_in_exception_with_last_error(self):
        mail = self._mail(missivus_retry_count=5)
        with GraphPostMock(token_ok(), graph_error(503, "ServiceUnavailable", "final straw")):
            mail.send()
        self.assertEqual(mail.state, "exception")
        self.assertEqual(mail.missivus_retry_count, 5)
        self.assertIn("final straw", mail.failure_reason)

    @mute_logger(MAIL_LOGGER)
    def test_permanent_error_is_not_retried(self):
        mail = self._mail()
        with GraphPostMock(token_ok(), graph_error(403, "ErrorAccessDenied", "denied")):
            mail.send()
        self.assertEqual(mail.state, "exception")
        self.assertEqual(mail.missivus_retry_count, 0)
        self.assertFalse(mail.scheduled_date)
        self.assertIn("ErrorAccessDenied", mail.failure_reason)

    def test_success_marks_sent(self):
        mail = self._mail()
        with GraphPostMock(token_ok(), ACCEPTED):
            mail.send()
        self.assertEqual(mail.state, "sent")

    @mute_logger(MAIL_LOGGER)
    def test_requeued_mail_is_picked_up_by_queue_after_scheduled_date(self):
        mail = self._mail()
        with GraphPostMock(token_ok(), graph_error(503, "ServiceUnavailable", "later")):
            mail.send()
        # Queue ignores it while scheduled in the future...
        with GraphPostMock(token_ok(), ACCEPTED) as post:
            self.env["mail.mail"].process_email_queue(email_ids=mail.ids)
            self.assertEqual(post.send_calls, [])
            # ...and sends it once the date has passed (retry_count unchanged on success)
            mail.scheduled_date = fields.Datetime.now() - timedelta(seconds=1)
            self.env["mail.mail"].process_email_queue(email_ids=mail.ids)
        self.assertEqual(mail.state, "sent")
        self.assertEqual(mail.missivus_retry_count, 1)

    @mute_logger(MAIL_LOGGER)
    def test_manual_retry_resets_counter(self):
        mail = self._mail(missivus_retry_count=5, state="exception")
        mail.action_retry()
        self.assertEqual(mail.state, "outgoing")
        self.assertEqual(mail.missivus_retry_count, 0)

    @mute_logger(MAIL_LOGGER)
    def test_smtp_server_failure_untouched(self):
        smtp = self.env["ir.mail_server"].create({"name": "smtp", "smtp_host": "mail.example.com"})
        mail = self._mail(mail_server_id=smtp.id, email_from="x@example.com")
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "nope")):
            mail.send()
        self.assertEqual(mail.state, "exception")
        self.assertEqual(mail.missivus_retry_count, 0)
        self.assertFalse(mail.scheduled_date)

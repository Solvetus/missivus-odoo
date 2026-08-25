# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import logging
from datetime import timedelta

from odoo import fields, models

from .ir_mail_server import pending_transient

_logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BACKOFF_MINUTES = (2, 4, 8, 16, 32)


class MailMail(models.Model):
    _inherit = "mail.mail"

    missivus_retry_count = fields.Integer(
        "Missivus retry count",
        default=0,
        copy=False,
        readonly=True,
        help="Transient Microsoft Graph failures re-queued so far for this mail.",
    )

    def _postprocess_sent_message(
        self, success_pids, success_emails, failure_reason=False, failure_type=None
    ):
        res = super()._postprocess_sent_message(
            success_pids, success_emails, failure_reason=failure_reason, failure_type=failure_type
        )
        error = getattr(pending_transient, "error", None)
        if error is None:
            return res
        pending_transient.error = None
        transient = self.filtered(
            lambda m: m.state == "exception" and m.message_id == error.message_id
        )
        for mail in transient:
            if mail.missivus_retry_count >= MAX_RETRIES:
                _logger.warning(
                    "mail.mail %s: giving up after %s transient Graph failures: %s",
                    mail.id,
                    mail.missivus_retry_count,
                    error,
                )
                continue
            attempt = mail.missivus_retry_count + 1
            delay = error.retry_after or BACKOFF_MINUTES[attempt - 1] * 60
            mail.write(
                {
                    "state": "outgoing",
                    "scheduled_date": fields.Datetime.now() + timedelta(seconds=delay),
                    "missivus_retry_count": attempt,
                }
            )
            _logger.info(
                "mail.mail %s: transient Graph failure, retry %s/%s in %ss",
                mail.id,
                attempt,
                MAX_RETRIES,
                delay,
            )
        return res

    def mark_outgoing(self):
        res = super().mark_outgoing()
        self.write({"missivus_retry_count": 0})
        return res

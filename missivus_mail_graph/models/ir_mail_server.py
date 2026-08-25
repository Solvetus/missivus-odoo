# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import logging
import re
import threading

from odoo import _, api, fields, models
from odoo.addons.base.models.ir_mail_server import (
    MailDeliveryException,
    extract_rfc2822_addresses,
)
from odoo.exceptions import ValidationError
from odoo.tools import email_normalize

from .. import graph_client
from ..graph_client import PermanentDeliveryError, TransientDeliveryError

_logger = logging.getLogger(__name__)
_test_logger = logging.getLogger("odoo.tests")

MISSIVUS_GRAPH = "missivus_graph"
GRAPH_MAX_EMAIL_MB = 3.0  # keep Odoo's attachment-to-link threshold under Graph's 4 MB request cap

# The TransientDeliveryError raised by the last Graph send on this thread. mail.mail's
# _postprocess_sent_message reads and clears it to re-queue the mail (see models/mail_mail.py).
pending_transient = threading.local()

EMAIL_SHAPE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

REQUIRED_GRAPH_FIELDS = (
    ("missivus_tenant_id", "Directory (tenant) ID"),
    ("missivus_client_id", "Application (client) ID"),
    ("missivus_client_secret", "Client secret"),
    ("missivus_sender", "Shared mailbox"),
)


class MissivusGraphSession:
    """Stand-in for the smtplib session Odoo threads through mail.mail.send() and send_email().

    Carries the two attributes _prepare_email_message__ reads (from_filter, smtp_from) and the
    no-op teardown methods mail.mail calls.
    """

    def __init__(self, server, smtp_from):
        self.server = server
        self.from_filter = server.from_filter
        self.smtp_from = smtp_from

    def quit(self):
        pass

    def close(self):
        pass


class IrMailServer(models.Model):
    _inherit = "ir.mail_server"

    smtp_authentication = fields.Selection(
        selection_add=[(MISSIVUS_GRAPH, "Missivus — Microsoft Graph")],
        ondelete={MISSIVUS_GRAPH: "set default"},
    )
    missivus_tenant_id = fields.Char("Directory (tenant) ID", groups="base.group_system")
    missivus_client_id = fields.Char("Application (client) ID", groups="base.group_system")
    missivus_client_secret = fields.Char("Client secret", groups="base.group_system")
    missivus_sender = fields.Char(
        "Shared mailbox",
        help="Address of the shared mailbox the app is allowed to send as, e.g. "
        "noreply@example.com. Saved as the FROM Filtering value so Odoo routes matching mail "
        "through this server.",
    )

    @api.depends("smtp_authentication")
    def _compute_smtp_authentication_info(self):
        graph = self.filtered(lambda s: s.smtp_authentication == MISSIVUS_GRAPH)
        if self - graph:
            super(IrMailServer, self - graph)._compute_smtp_authentication_info()
        for server in graph:
            server.smtp_authentication_info = _(
                "Send through Microsoft Graph with application permissions and a shared mailbox. "
                "No SMTP host, no user login: the app registration authenticates with its client "
                "secret and sends as the shared mailbox."
            )

    @api.constrains("smtp_authentication", *(name for name, _label in REQUIRED_GRAPH_FIELDS))
    def _check_missivus_graph(self):
        for server in self.filtered(lambda s: s.smtp_authentication == MISSIVUS_GRAPH).sudo():
            missing = [label for name, label in REQUIRED_GRAPH_FIELDS if not server[name]]
            if missing:
                raise ValidationError(_("Missivus — Microsoft Graph needs: %s", ", ".join(missing)))
            sender = server.missivus_sender.strip()
            if not EMAIL_SHAPE.fullmatch(sender) or not email_normalize(sender):
                raise ValidationError(
                    _("'%s' is not a valid email address for the shared mailbox.", sender)
                )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("smtp_authentication") == MISSIVUS_GRAPH and vals.get("missivus_sender"):
                vals["from_filter"] = vals["missivus_sender"].strip()
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        if {"smtp_authentication", "missivus_sender", "from_filter"} & vals.keys():
            for server in self.filtered(lambda s: s.smtp_authentication == MISSIVUS_GRAPH):
                sender = (server.missivus_sender or "").strip()
                if sender and server.from_filter != sender:
                    super(IrMailServer, server).write({"from_filter": sender})
        return res

    # ------------------------------------------------------------------
    # Session + send path
    # ------------------------------------------------------------------
    def _missivus_graph_session(self, mail_server_id, smtp_from):
        """Return a MissivusGraphSession when the server handling `smtp_from` is a Graph server."""
        if mail_server_id:
            server = self.sudo().browse(mail_server_id)
        else:
            server, smtp_from = self.sudo()._find_mail_server(smtp_from)
        if server and server.smtp_authentication == MISSIVUS_GRAPH:
            return MissivusGraphSession(server, smtp_from)
        return None

    def _connect__(
        self,
        host=None,
        port=None,
        user=None,
        password=None,
        encryption=None,
        smtp_from=None,
        ssl_certificate=None,
        ssl_private_key=None,
        smtp_debug=False,
        mail_server_id=None,
        allow_archived=False,
    ):
        if not host:
            session = self._missivus_graph_session(mail_server_id, smtp_from)
            if session:
                if mail_server_id:
                    self._check_forced_mail_server(session.server, allow_archived, smtp_from)
                return session
        return super()._connect__(
            host=host,
            port=port,
            user=user,
            password=password,
            encryption=encryption,
            smtp_from=smtp_from,
            ssl_certificate=ssl_certificate,
            ssl_private_key=ssl_private_key,
            smtp_debug=smtp_debug,
            mail_server_id=mail_server_id,
            allow_archived=allow_archived,
        )

    @api.model
    def send_email(
        self,
        message,
        mail_server_id=None,
        smtp_server=None,
        smtp_port=None,
        smtp_user=None,
        smtp_password=None,
        smtp_encryption=None,
        smtp_ssl_certificate=None,
        smtp_ssl_private_key=None,
        smtp_debug=False,
        smtp_session=None,
    ):
        session = smtp_session
        if session is None and not smtp_server:
            session = self._missivus_graph_session(mail_server_id, message["From"])
        if isinstance(session, MissivusGraphSession):
            return self._missivus_send_email(message, session)
        return super().send_email(
            message,
            mail_server_id=mail_server_id,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            smtp_encryption=smtp_encryption,
            smtp_ssl_certificate=smtp_ssl_certificate,
            smtp_ssl_private_key=smtp_ssl_private_key,
            smtp_debug=smtp_debug,
            smtp_session=smtp_session,
        )

    def _missivus_send_email(self, message, session):
        server = session.server
        pending_transient.error = None
        # Same preparation Odoo applies before SMTP: From rewrite per from_filter, To/Cc/Bcc
        # extraction, Bcc header removal, header cleanup.
        _smtp_from, smtp_to_list, message = self._prepare_email_message__(message, session)

        if self._disable_send():
            _test_logger.debug("skip sending email in test mode")
            return message["Message-Id"]

        # Graph raw MIME takes recipients from the headers, so put back the envelope-only ones.
        header_recipients = {
            address
            for header in ("To", "Cc")
            for address in extract_rfc2822_addresses(message[header])
        }
        hidden = [address for address in smtp_to_list if address not in header_recipients]
        if hidden:
            message["Bcc"] = ", ".join(hidden)

        try:
            graph_client.send_raw_mime(
                server.missivus_sender,
                message.as_bytes(),
                server.missivus_tenant_id,
                server.missivus_client_id,
                server.missivus_client_secret,
            )
        except TransientDeliveryError as exc:
            exc.message_id = message["Message-Id"]
            pending_transient.error = exc
            _logger.info(
                "Transient Graph failure for %s via '%s': %s", exc.message_id, server.name, exc
            )
            raise
        except PermanentDeliveryError as exc:
            msg = _(
                "Mail delivery failed via Missivus — Microsoft Graph server '%(server)s'.\n"
                "%(error)s",
                server=server.name,
                error=exc,
            )
            _logger.info(msg)
            raise MailDeliveryException(_("Mail Delivery Failed"), msg) from exc
        return message["Message-Id"]

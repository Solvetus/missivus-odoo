# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import logging
import re
import threading

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)

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

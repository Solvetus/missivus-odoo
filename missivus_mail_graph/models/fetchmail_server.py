# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .missivus_graph_mixin import MISSIVUS_GRAPH, REQUIRED_GRAPH_FIELDS

_logger = logging.getLogger(__name__)

POST_PROCESS_MARK_READ = "mark_read"
POST_PROCESS_MOVE = "move"


class FetchmailServer(models.Model):
    _name = "fetchmail.server"
    _inherit = ["fetchmail.server", "missivus.graph.mixin"]

    server_type = fields.Selection(
        selection_add=[(MISSIVUS_GRAPH, "Missivus — Microsoft Graph")],
        ondelete={MISSIVUS_GRAPH: "set default"},
    )
    missivus_sender = fields.Char(
        "Shared mailbox",
        help="Address of the shared mailbox to fetch from, e.g. inbox@example.com. The app's "
        "application access policy must cover it.",
    )
    missivus_folder = fields.Char(
        "Folder",
        default="inbox",
        help="Well-known name (inbox, archive, junkemail, ...) or the display name of a folder.",
    )
    missivus_post_process = fields.Selection(
        [(POST_PROCESS_MARK_READ, "Mark as read"), (POST_PROCESS_MOVE, "Move to folder")],
        "After processing",
        default=POST_PROCESS_MARK_READ,
        help="Applied only after Odoo processed the message successfully. Messages that fail "
        "processing are moved to the 'Missivus Quarantine' folder regardless of this setting.",
    )
    missivus_move_folder = fields.Char(
        "Move to", help="Display name of the target folder; created if it does not exist."
    )
    missivus_batch_cap = fields.Integer(
        "Messages per run",
        default=50,
        help="Maximum unread messages fetched in one cron run. The rest wait for the next run.",
    )

    @api.depends("server_type")
    def _compute_server_type_info(self):
        graph = self.filtered(lambda s: s.server_type == MISSIVUS_GRAPH)
        graph.server_type_info = _(
            "Fetch unread mail from a shared mailbox through Microsoft Graph with application "
            "permissions. No IMAP host, no user login: the app registration authenticates with "
            "its client secret and reads the mailbox its access policy allows."
        )
        super(FetchmailServer, self - graph)._compute_server_type_info()

    @api.onchange("server_type")
    def onchange_server_type(self):
        if self.server_type == MISSIVUS_GRAPH:
            self.port = 0
            self.server = False
            self.is_ssl = False
        else:
            super().onchange_server_type()

    @api.constrains(
        "server_type",
        "missivus_folder",
        "missivus_post_process",
        "missivus_move_folder",
        "missivus_batch_cap",
        *(name for name, _label in REQUIRED_GRAPH_FIELDS),
    )
    def _check_missivus_graph(self):
        graph = self.filtered(lambda s: s.server_type == MISSIVUS_GRAPH)
        graph._missivus_check_config()
        for server in graph:
            if not (server.missivus_folder or "").strip():
                raise ValidationError(_("Missivus — Microsoft Graph needs a folder to fetch from."))
            if server.missivus_batch_cap <= 0:
                raise ValidationError(_("Messages per run must be greater than zero."))
            if (
                server.missivus_post_process == POST_PROCESS_MOVE
                and not (server.missivus_move_folder or "").strip()
            ):
                raise ValidationError(_("'Move to folder' needs a target folder name."))

    def button_confirm_login(self):
        graph = self.filtered(lambda s: s.server_type == MISSIVUS_GRAPH)
        if self - graph:
            super(FetchmailServer, self - graph).button_confirm_login()
        if graph:
            # Token proof only, never a fetch: policy coverage is proven by the first real run.
            graph._missivus_test_token()
            graph.write({"state": "done"})
        return True

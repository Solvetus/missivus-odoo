# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Shared Microsoft Graph app-registration config for outgoing and incoming mail servers.

Field names are identical on every model that inherits this mixin so the columns v0.1.0 created
on ir_mail_server are reused as-is (no migration).
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import email_normalize

from .. import graph_client
from ..graph_client import GraphError

MISSIVUS_GRAPH = "missivus_graph"
EMAIL_SHAPE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
REQUIRED_GRAPH_FIELDS = (
    ("missivus_tenant_id", "Directory (tenant) ID"),
    ("missivus_client_id", "Application (client) ID"),
    ("missivus_client_secret", "Client secret"),
    ("missivus_sender", "Shared mailbox"),
)


class MissivusGraphMixin(models.AbstractModel):
    _name = "missivus.graph.mixin"
    _description = "Missivus — Microsoft Graph configuration"

    missivus_tenant_id = fields.Char("Directory (tenant) ID", groups="base.group_system")
    missivus_client_id = fields.Char("Application (client) ID", groups="base.group_system")
    missivus_client_secret = fields.Char("Client secret", groups="base.group_system")
    # Send-as vs fetch-from semantics differ per model: each inheriting model overrides the
    # label/help, never the name.
    missivus_sender = fields.Char("Shared mailbox")

    def _missivus_check_config(self):
        """Validate the four fields on records already known to be Graph-typed."""
        for record in self.sudo():
            missing = [label for name, label in REQUIRED_GRAPH_FIELDS if not record[name]]
            if missing:
                raise ValidationError(_("Missivus — Microsoft Graph needs: %s", ", ".join(missing)))
            mailbox = record.missivus_sender.strip()
            if not EMAIL_SHAPE.fullmatch(mailbox) or not email_normalize(mailbox):
                raise ValidationError(
                    _("'%s' is not a valid email address for the shared mailbox.", mailbox)
                )

    def _missivus_test_token(self):
        """Prove the saved credentials by acquiring a fresh token. Never touches a mailbox.

        Token proof is not policy proof: whether the mailbox exists and the application access
        policy covers it is only proven by a real send or fetch.
        """
        for record in self.sudo():
            try:
                # force_refresh: the button must prove the credentials as saved, not a cached token
                graph_client.get_token(
                    record.missivus_tenant_id,
                    record.missivus_client_id,
                    record.missivus_client_secret,
                    force_refresh=True,
                )
            except GraphError as exc:
                raise UserError(
                    _(
                        "Microsoft Entra did not issue a token for '%(server)s'.\n%(error)s",
                        server=record.name,
                        error=exc,
                    )
                ) from exc

    @api.model
    def _missivus_token_ok_message(self):
        return _(
            "Token acquired from Microsoft Entra. Whether the shared mailbox and the access "
            "policy are right is only proven by a real send: this test never sends mail."
        )

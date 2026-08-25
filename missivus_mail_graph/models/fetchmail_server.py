# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
import functools
import logging

from odoo import _, api, fields, models
from odoo.addons.mail.models.fetchmail import MAIL_SERVER_DEACTIVATE_TIME
from odoo.exceptions import ValidationError
from odoo.tools import exception_to_unicode

from .. import graph_client
from ..graph_client import (
    QUARANTINE_FOLDER,
    GraphError,
    NotFoundError,
    TransientDeliveryError,
)
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

    # ------------------------------------------------------------------
    # Fetch loop — mirrors mail/models/fetchmail.py:_fetch_mail transaction for transaction
    # ------------------------------------------------------------------
    def _fetch_mail(self, batch_limit=50):
        graph = self.filtered(lambda s: s.server_type == MISSIVUS_GRAPH)
        result_exception = None
        if self - graph:
            result_exception = super(FetchmailServer, self - graph)._fetch_mail(batch_limit)
        for server in graph.with_context(fetchmail_cron_running=True):
            exc = server._missivus_fetch_server(batch_limit)
            result_exception = result_exception or exc
        return result_exception

    def _missivus_creds(self):
        self.ensure_one()
        return (self.missivus_tenant_id, self.missivus_client_id, self.missivus_client_secret)

    def _missivus_fetch_server(self, batch_limit):
        self.ensure_one()
        if not self.try_lock_for_update(allow_referencing=True).filtered_domain(
            [("state", "=", "done"), ("server_type", "=", MISSIVUS_GRAPH)]
        ):
            _logger.info("Skip checking for new mails on mail server id %d (unavailable)", self.id)
            return None
        name = self.name
        mailbox = self.missivus_sender.strip()
        creds = self._missivus_creds()
        top = min(self.missivus_batch_cap, batch_limit)
        _logger.info("Start checking for new emails on Missivus Graph server %s", name)
        count = failed = 0
        result_exception = None
        message_cr = None
        quarantine = {}  # folder id, resolved lazily once per run
        try:
            message_cr = self.env.registry.cursor()
            MailThread = (
                self.env["mail.thread"]
                .with_env(self.env(cr=message_cr))
                .with_context(default_fetchmail_server_id=self.id)
            )
            thread_process_message = functools.partial(
                MailThread.message_process,
                model=self.object_id.model,
                save_original=self.original,
                strip_attachments=(not self.attach),
            )
            folder_id = graph_client.resolve_folder(mailbox, self.missivus_folder, *creds)
            target_id = None
            if self.missivus_post_process == POST_PROCESS_MOVE:
                target_id = graph_client.ensure_folder(
                    mailbox, self.missivus_move_folder.strip(), *creds
                )
            message_ids = graph_client.list_unread_message_ids(mailbox, folder_id, top, *creds)
            _logger.debug("%d unread messages on Missivus Graph server %s.", len(message_ids), name)
            remaining_time = True
            for message_id in message_ids:
                try:
                    raw = graph_client.fetch_raw_mime(mailbox, message_id, *creds)
                except NotFoundError:
                    _logger.info(
                        "Message %s vanished from %s before fetch on server %s; skipping",
                        message_id,
                        mailbox,
                        name,
                    )
                    continue
                count += 1
                try:
                    thread_process_message(message=raw)
                    remaining_time = MailThread.env["ir.cron"]._commit_progress(1)
                except Exception as exc:  # noqa: BLE001 — same breadth as the native loop
                    MailThread.env.cr.rollback()
                    failed += 1
                    remaining_time = MailThread.env["ir.cron"]._commit_progress(1)
                    _logger.error(
                        "Missivus: message %s on server %s failed processing (%s); "
                        "moving to quarantine",
                        message_id,
                        name,
                        exc.__class__.__name__,
                    )
                    self._missivus_quarantine(mailbox, message_id, creds, quarantine)
                else:
                    self._missivus_post_process(mailbox, message_id, target_id, creds)
                if count >= batch_limit or not remaining_time:
                    break
            self.error_date = False
            self.error_message = False
        except Exception as e:  # noqa: BLE001
            # Transient Graph errors, folder-not-found and anything unexpected: abort this
            # server's run; unprocessed mail stays unread for the next run. Same bookkeeping
            # as core.
            result_exception = e
            _logger.info(
                "General failure when trying to fetch mail from Missivus Graph server %s.",
                name,
                exc_info=True,
            )
            if not self.error_date:
                self.error_date = fields.Datetime.now()
                self.error_message = exception_to_unicode(e)
            elif self.error_date < fields.Datetime.now() - MAIL_SERVER_DEACTIVATE_TIME:
                message = f"Deactivating fetchmail missivus_graph server {name} (too many failures)"
                self.set_draft()
                self.env["ir.cron"]._notify_admin(message)
        finally:
            if message_cr is not None:
                message_cr.close()
        _logger.info(
            "Fetched %d email(s) on Missivus Graph server %s; %d succeeded, %d failed.",
            count,
            name,
            count - failed,
            failed,
        )
        self.write({"date": fields.Datetime.now()})
        self.env.cr.commit()
        self.env["ir.cron"]._commit_progress(1)
        return result_exception

    def _missivus_post_process(self, mailbox, message_id, target_id, creds):
        """Only reached after a successful message_process.

        Transient errors propagate (abort the run: the message stays unread and is deduplicated
        by Message-Id next time); permanent errors are logged and the run continues.
        """
        try:
            if target_id:
                graph_client.move_message(mailbox, message_id, target_id, *creds)
            else:
                graph_client.mark_read(mailbox, message_id, *creds)
        except TransientDeliveryError:
            raise
        except GraphError as exc:
            _logger.error(
                "Missivus: message %s processed but post-processing failed (%s: %s)",
                message_id,
                exc.__class__.__name__,
                exc,
            )

    def _missivus_quarantine(self, mailbox, message_id, creds, cache):
        """Move a poison message to the quarantine folder.

        Never raises: a failure here is logged and the message is left where it is for the
        next run.
        """
        try:
            if "id" not in cache:
                cache["id"] = graph_client.ensure_folder(mailbox, QUARANTINE_FOLDER, *creds)
            graph_client.move_message(mailbox, message_id, cache["id"], *creds)
        except GraphError as exc:
            _logger.error(
                "Missivus: could not quarantine message %s on %s (%s: %s); "
                "left in place for the next run",
                message_id,
                mailbox,
                exc.__class__.__name__,
                exc,
            )

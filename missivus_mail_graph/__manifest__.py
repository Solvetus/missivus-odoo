# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
{
    "name": "Missivus — Microsoft Graph mail",
    "summary": (
        "Send and receive mail through Microsoft Graph with application permissions "
        "and shared mailboxes. No SMTP, no IMAP, no user login."
    ),
    "version": "19.0.0.2.0",
    "category": "Technical",
    "author": "Solvetus",
    "website": "https://missivus.com",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": ["views/ir_mail_server_views.xml", "views/fetchmail_server_views.xml"],
    "installable": True,
    "application": False,
}

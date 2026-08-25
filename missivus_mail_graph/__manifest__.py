# SPDX-FileCopyrightText: 2026 Solvetus
# SPDX-License-Identifier: LGPL-3.0-or-later
{
    "name": "Missivus — Microsoft Graph outgoing mail",
    "summary": (
        "Send all outgoing mail through Microsoft Graph with application permissions "
        "and a shared mailbox. No SMTP, no user login."
    ),
    "version": "19.0.1.0.0",
    "category": "Technical",
    "author": "Solvetus",
    "website": "https://missivus.com",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": ["views/ir_mail_server_views.xml"],
    "installable": True,
    "application": False,
}

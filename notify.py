"""Email a pipeline failure via Gmail SMTP.

Credentials come from the environment (GitHub secrets): SMTP_USER and
SMTP_APP_PASSWORD - a Gmail app password. Recipients: NOTIFY_TO, comma-separated.

    python notify.py failure "Shopee BQ pipeline failed" "<details / run URL>"
    python notify.py test
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage

DEFAULT_TO = "brian.rinaldy@lmbg.co.id"


ASSIGNEE = "brianirsyad-scarlett"


def _send_via_issue(subject, body, attachment=None):
    """Queue the mail as a MAIL| issue assigned to the owner; a Power Automate flow ("When a new issue
    is opened and assigned to me" -> Outlook "Send an email") sends it. No SMTP password needed."""
    import json as _json
    import urllib.request as _rq
    token, repo = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    if attachment and os.path.exists(str(attachment)):
        body += chr(10) * 2 + open(str(attachment), encoding="utf-8", errors="replace").read()[:20000]
    req = _rq.Request(
        f"https://api.github.com/repos/{repo}/issues", method="POST",
        data=_json.dumps({"title": f"MAIL|{subject}", "body": body, "assignees": [ASSIGNEE]}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with _rq.urlopen(req, timeout=60) as resp:
            print(f"queued mail issue #{_json.load(resp)['number']}: {subject}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::could not open the mail issue: {exc}")
        return False



def send(subject: str, body: str) -> None:
    if _send_via_issue(subject, body, None):
        return None
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_APP_PASSWORD")
    if not (user and pw):
        print("SMTP_USER / SMTP_APP_PASSWORD not set - skipping failure email.")
        return
    to = [a.strip() for a in os.environ.get("NOTIFY_TO", DEFAULT_TO).split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"sent '{subject}' to {', '.join(to)}")


def main(argv) -> int:
    run_url = os.environ.get("RUN_URL", "")
    if argv[0] == "failure":
        send(f"[SOM pipeline] {argv[1]}", f"{argv[2] if len(argv) > 2 else ''}\n\n{run_url}")
    elif argv[0] == "test":
        send("[SOM pipeline] Test - Shopee BQ pipeline alert",
             f"This is a test of the Shopee BQ parquet pipeline failure alert.\n\n{run_url}")
    else:
        raise SystemExit(f"unknown command {argv[0]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Preview or send a plain-text digest for upcoming review deadlines."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_URL = "https://api.github.com/graphql"
REPOSITORY = "alxlys/code-review-deadline-demo"
LABEL = "review deadline"
TIME_ZONE = ZoneInfo("Europe/Sofia")

QUERY = """
query($after: String) {
  user(login: "alxlys") {
    projectV2(number: 1) {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isArchived
          content {
            __typename
            ... on PullRequest {
              number
              title
              url
              state
              isDraft
              reviewDecision
              repository { nameWithOwner }
              labels(first: 100) { nodes { name } }
            }
          }
          fieldValueByName(name: "Review by") {
            ... on ProjectV2ItemFieldDateValue { date }
          }
        }
      }
    }
  }
}
"""


def graphql(token: str, after: str | None) -> dict[str, Any]:
    payload = json.dumps({"query": QUERY, "variables": {"after": after}}).encode()
    request = Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError):
        raise RuntimeError("GitHub Projects query failed; check token access and connectivity") from None
    if result.get("errors"):
        raise RuntimeError("GitHub Projects query failed; check token access and project visibility")
    project = result.get("data", {}).get("user", {}).get("projectV2")
    if project is None:
        raise RuntimeError("GitHub Project alxlys/#1 was not found or is not readable")
    return project["items"]


def fetch_items(token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = graphql(token, cursor)
        items.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


def window(today: date) -> tuple[date, date]:
    end = today
    remaining = 2
    while remaining:
        end += timedelta(days=1)
        if end.weekday() < 5:
            remaining -= 1
    return today, end


def qualifying_rows(items: list[dict[str, Any]], start: date, end: date) -> list[tuple[date, int, str, str]]:
    rows = []
    for item in items:
        pr = item.get("content")
        if item.get("isArchived") or not pr or pr.get("__typename") != "PullRequest":
            continue
        if pr.get("repository", {}).get("nameWithOwner") != REPOSITORY:
            continue
        if pr.get("state") != "OPEN" or pr.get("isDraft") or pr.get("reviewDecision") == "APPROVED":
            continue
        labels = {label["name"].casefold() for label in pr.get("labels", {}).get("nodes", [])}
        if LABEL.casefold() not in labels:
            continue

        value = item.get("fieldValueByName")
        due = date.fromisoformat(value["date"]) if value and value.get("date") else None
        if due is None or due < start or due > end:
            continue
        rows.append((due, pr["number"], pr["title"], pr["url"]))
    return sorted(rows, key=lambda row: (row[0], row[1]))


def render(rows: list[tuple[date, int, str, str]], start: date, end: date) -> str:
    if not rows:
        return f"No qualifying review deadlines from {start.isoformat()} through {end.isoformat()}."
    lines = [
        "Upcoming code review deadlines",
        f"Window: {start.isoformat()} through {end.isoformat()} (Europe/Sofia)",
        "",
    ]
    for due, number, title, url in rows:
        lines.append(f"{due.isoformat()}  #{number}  {title}")
        lines.append(f"  {url}")
    return "\n".join(lines)


def send_email(body: str) -> None:
    names = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO")
    config = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in config.items() if not value]
    if missing:
        raise RuntimeError("Email configuration is incomplete: " + ", ".join(missing))
    try:
        port = int(config["SMTP_PORT"])
    except ValueError:
        raise RuntimeError("SMTP_PORT must be 465 or 587") from None
    if port not in (465, 587):
        raise RuntimeError("SMTP_PORT must be 465 or 587")

    message = EmailMessage()
    message["Subject"] = "Upcoming code review deadlines"
    message["From"] = os.environ.get("SMTP_FROM", "").strip() or config["SMTP_USERNAME"]
    message["To"] = config["MAIL_TO"]
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(config["SMTP_HOST"], port, context=context, timeout=30) as server:
                server.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
                server.send_message(message)
        else:
            with smtplib.SMTP(config["SMTP_HOST"], port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
                server.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise RuntimeError("Email delivery failed; check SMTP settings and provider logs") from None


def main() -> int:
    token = os.environ.get("PROJECTS_READ_TOKEN", "").strip()
    if not token:
        print("PROJECTS_READ_TOKEN is required to read the project", file=sys.stderr)
        return 2
    today = datetime.now(TIME_ZONE).date()
    start, end = window(today)
    try:
        rows = qualifying_rows(fetch_items(token), start, end)
        body = render(rows, start, end)
        if not rows:
            print(body)
            print("Email skipped because there are no matching items.")
        elif os.environ.get("SEND_EMAIL", "false").casefold() == "true":
            send_email(body)
            print("Digest email sent.")
        else:
            print("Preview only; no email sent.\n")
            print(body)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

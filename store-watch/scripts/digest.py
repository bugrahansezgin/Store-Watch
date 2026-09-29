#!/usr/bin/env python3
"""Weekly Slack digest of the last 7 days of changes.

Env:
  SLACK_WEBHOOK_URL  Slack incoming webhook (required to send)
  PANEL_URL          Link to the GitHub Pages panel (optional)
  DIGEST_DAYS        Window in days (default 7)
  DRY_RUN=1          Print the payload instead of sending
"""
import datetime as dt
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOJI = {"screenshots": "🖼️", "release": "📦", "icon": "🎨", "price": "🏷️",
         "title": "✏️", "description": "📝", "rating": "⭐", "tracking": "👀"}
ORDER = ["screenshots", "icon", "title", "price", "release", "description", "rating", "tracking"]


def main():
    days = int(os.environ.get("DIGEST_DAYS", "7"))
    panel = os.environ.get("PANEL_URL", "").rstrip("/")
    feed = json.load(open(os.path.join(ROOT, "data", "feed.json")))
    apps = {f"{a['country']}-{a['id']}": a for a in feed["apps"]}
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    recent = [e for e in feed["events"]
              if dt.datetime.fromisoformat(e["ts"]) >= since and e["type"] != "tracking"]

    header = f"App Store watch · last {days} days"
    if not recent:
        text = f"*{header}*\nNo changes across {len(apps)} tracked apps."
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    else:
        by_app = {}
        for e in recent:
            by_app.setdefault(e["app"], []).append(e)
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": f"*{len(recent)}* changes · *{len(by_app)}* of {len(apps)} apps"}]},
        ]
        # Apps with visual changes first
        def weight(item):
            return min(ORDER.index(e["type"]) for e in item[1])
        for key, evs in sorted(by_app.items(), key=weight):
            app = apps.get(key, {})
            evs.sort(key=lambda e: ORDER.index(e["type"]))
            lines = []
            for e in evs:
                link = f"{panel}/#event/{e['id']}" if panel else None
                label = e["type"].capitalize()
                line = f"{EMOJI.get(e['type'], '•')} *{label}* — {e['summary']}"
                if link:
                    line += f"  <{link}|View>"
                lines.append(line)
            section = {"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*{app.get('name', key)}*\n" + "\n".join(lines)}}
            if app.get("icon"):
                section["accessory"] = {"type": "image", "image_url": app["icon"], "alt_text": app.get("name", key)}
            blocks.append(section)
        text = f"{header}: {len(recent)} changes"
    if panel:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Open panel"}, "url": panel}]})

    payload = {"text": text, "blocks": blocks}
    if os.environ.get("DRY_RUN") or not os.environ.get("SLACK_WEBHOOK_URL"):
        print(json.dumps(payload, indent=1, ensure_ascii=False))
        if not os.environ.get("DRY_RUN"):
            print("SLACK_WEBHOOK_URL not set — nothing sent.", file=sys.stderr)
        return
    req = urllib.request.Request(os.environ["SLACK_WEBHOOK_URL"], data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("slack:", r.status, r.read().decode())


if __name__ == "__main__":
    main()

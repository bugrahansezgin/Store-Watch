#!/usr/bin/env python3
"""Stop tracking an app and delete its data.
Usage: remove_app.py <App Store / Google Play link | App Store id | package name> [country]
Without a country, every country entry of that app is removed."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track import DATA, EVENTS_FILE, ROOT, app_key, load, parse_app, rebuild_feed, save  # noqa: E402

PATH = os.path.join(ROOT, "apps.json")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ref = sys.argv[1].strip()
    country = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
    play = re.search(r"play\.google\.com/store/apps/details\?(?:[^#]*&)?id=([A-Za-z0-9_.]+)", ref)
    ios = re.search(r"id(\d{6,})", ref) or re.fullmatch(r"(\d{6,})", ref)
    target = play.group(1) if play else ios.group(1) if ios else ref

    data = json.load(open(PATH))
    keep, removed = [], []
    for entry in data["apps"]:
        app_id, c, platform = parse_app(entry)
        if app_id == target and (not country or c == country):
            removed.append(app_key(app_id, c, platform))
        else:
            keep.append(entry)
    if not removed:
        sys.exit(f"Not tracking {target}{' in ' + country if country else ''} — nothing removed.")
    data["apps"] = keep
    with open(PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    for key in removed:
        for folder in ("state", "reviews", "history", "summaries", "versions_ai"):
            p = os.path.join(DATA, folder, f"{key}.json")
            if os.path.exists(p):
                os.remove(p)
    events = [e for e in load(EVENTS_FILE, []) if e.get("app") not in removed]
    save(EVENTS_FILE, events)
    rebuild_feed()
    print("Removed: " + ", ".join(removed))


if __name__ == "__main__":
    main()

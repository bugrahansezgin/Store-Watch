#!/usr/bin/env python3
"""Add an app to apps.json.  Usage: add_app.py <app-store-url-or-id> [country]"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "apps.json")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ref = sys.argv[1].strip()
    m = re.search(r"id(\d{6,})", ref) or re.fullmatch(r"(\d{6,})", ref)
    if not m:
        sys.exit(f"Couldn't find an App Store id in: {ref}")
    app_id = m.group(1)
    cm = re.search(r"apps\.apple\.com/([a-z]{2})/", ref)
    country = (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else (cm.group(1) if cm else "us")).lower()
    data = json.load(open(PATH))
    if any(str(a.get("id")) == app_id and a.get("country", "us") == country for a in data["apps"]):
        print(f"Already tracking {country}-{app_id}")
        return
    note = ""
    sm = re.search(r"/app/([^/]+)/id", ref)
    if sm:
        note = sm.group(1).replace("-", " ")
    data["apps"].append({"id": app_id, "country": country, "note": note})
    with open(PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Added {country}-{app_id}")


if __name__ == "__main__":
    main()

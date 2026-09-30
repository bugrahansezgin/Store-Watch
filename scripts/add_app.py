#!/usr/bin/env python3
"""Add an app to apps.json.
Usage: add_app.py <App Store or Google Play link | App Store id | package name> [country]"""
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
    forced_country = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
    play = re.search(r"play\.google\.com/store/apps/details\?(?:[^#]*&)?id=([A-Za-z0-9_.]+)", ref)
    if play or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", ref):
        app_id, platform = (play.group(1) if play else ref), "android"
        g = re.search(r"[?&]gl=([A-Za-z]{2})", ref)
        country = forced_country or (g.group(1).lower() if g else "us")
        note = app_id
    else:
        m = re.search(r"id(\d{6,})", ref) or re.fullmatch(r"(\d{6,})", ref)
        if not m:
            sys.exit(f"Couldn't find an App Store or Google Play id in: {ref}")
        app_id, platform = m.group(1), "ios"
        cm = re.search(r"apps\.apple\.com/([a-z]{2})/", ref)
        country = forced_country or (cm.group(1) if cm else "us")
        sm = re.search(r"/app/([^/]+)/id", ref)
        note = sm.group(1).replace("-", " ") if sm else ""
    data = json.load(open(PATH))
    for a in data["apps"]:
        a_platform = a.get("platform") or ("ios" if str(a.get("id", "")).isdigit() else "android")
        if str(a.get("id")) == app_id and a.get("country", "us") == country and a_platform == platform:
            print(f"Already tracking {platform} {country}-{app_id}")
            return
    data["apps"].append({"id": app_id, "country": country, "platform": platform, "note": note})
    with open(PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Added {platform} {country}-{app_id}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Daily App Store listing tracker.

Reads apps.json, fetches each listing from the public iTunes Lookup API,
diffs it against the last saved state and appends change events.

Outputs (committed back to the repo by the workflow):
  data/state/<country>-<id>.json   latest snapshot per app
  data/events.json                 append-only change log (newest first)
  data/feed.json                   what the panel reads (apps + events)

Stdlib only. Set FIXTURE_DIR to read lookup JSON from disk (for tests).
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "state")
EVENTS_FILE = os.path.join(DATA, "events.json")
FEED_FILE = os.path.join(DATA, "feed.json")
MAX_EVENTS = 2000
RATING_THRESHOLD = 0.05

ID_RE = re.compile(r"id(\d{6,})")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def parse_app(entry):
    """Accept {id, country} or {url} entries."""
    app_id = str(entry.get("id") or "")
    country = (entry.get("country") or "").lower()
    url = entry.get("url") or ""
    if not app_id and url:
        m = ID_RE.search(url)
        app_id = m.group(1) if m else ""
    if not country and url:
        m = re.search(r"apps\.apple\.com/([a-z]{2})/", url)
        country = m.group(1) if m else "us"
    return app_id, country or "us"


def fetch(app_id, country):
    fixture_dir = os.environ.get("FIXTURE_DIR")
    if fixture_dir:
        return load(os.path.join(fixture_dir, f"{country}-{app_id}.json"), None)
    url = f"https://itunes.apple.com/lookup?id={app_id}&country={country}&entity=software"
    req = urllib.request.Request(url, headers={"User-Agent": "store-watch/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.load(r)
            results = payload.get("results") or []
            return results[0] if results else None
        except Exception as e:  # network hiccup / 403 rate limit
            print(f"  fetch failed ({attempt + 1}/3): {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return None


def shot_key(url):
    """mzstatic URLs end with a size segment (…/392x696bb.png). Strip it so
    the same asset served at a different size isn't seen as a new image."""
    return url.rsplit("/", 1)[0] if url.count("/") > 3 else url


def snapshot(raw, app_id, country):
    desc = raw.get("description") or ""
    return {
        "id": app_id,
        "country": country,
        "name": raw.get("trackName"),
        "developer": raw.get("artistName") or raw.get("sellerName"),
        "url": (raw.get("trackViewUrl") or "").split("?")[0],
        "icon": raw.get("artworkUrl512") or raw.get("artworkUrl100"),
        "version": raw.get("version"),
        "releaseNotes": raw.get("releaseNotes"),
        "versionDate": raw.get("currentVersionReleaseDate"),
        "price": raw.get("formattedPrice"),
        "rating": round(raw.get("averageUserRatingForCurrentVersion") or raw.get("averageUserRating") or 0, 2),
        "ratingCount": raw.get("userRatingCount"),
        "genre": raw.get("primaryGenreName"),
        "descriptionHash": hashlib.sha1(desc.encode()).hexdigest()[:12],
        "description": desc,
        "screenshots": raw.get("screenshotUrls") or [],
        "ipadScreenshots": raw.get("ipadScreenshotUrls") or [],
    }


def diff_shots(before, after):
    bk = [shot_key(u) for u in before]
    ak = [shot_key(u) for u in after]
    bset, aset = set(bk), set(ak)
    added = [i for i, k in enumerate(ak) if k not in bset]
    removed = [i for i, k in enumerate(bk) if k not in aset]
    moved = [
        {"key": k, "from": bk.index(k), "to": i}
        for i, k in enumerate(ak)
        if k in bset and bk.index(k) != i
    ]
    # Same slot lost an old image and gained a new one -> "replaced"
    replaced = sorted(set(added) & set(removed))
    return {
        "added": added,          # indexes in `after`
        "removed": removed,      # indexes in `before`
        "replaced": replaced,    # slot indexes present in both lists above
        "moved": moved,
        "countBefore": len(before),
        "countAfter": len(after),
    }


def shots_summary(d):
    rep = len(d["replaced"])
    add = len(d["added"]) - rep
    rem = len(d["removed"]) - rep
    parts = []
    if rep:
        parts.append(f"{rep} replaced")
    if add:
        parts.append(f"{add} added")
    if rem:
        parts.append(f"{rem} removed")
    if d["moved"]:
        parts.append(f"{len(d['moved'])} reordered")
    s = ", ".join(parts)
    return s[:1].upper() + s[1:] + f" · {d['countBefore']} → {d['countAfter']} total"


def compare(old, new):
    """Yield (type, title, summary, extra) tuples."""
    name = new["name"]
    if old["version"] != new["version"]:
        yield ("release", f"{name} shipped version {new['version']}",
               f"{old['version']} → {new['version']}",
               {"before": old["version"], "after": new["version"], "notes": new.get("releaseNotes")})
    for field, label in (("screenshots", "iPhone"), ("ipadScreenshots", "iPad")):
        if [shot_key(u) for u in old[field]] != [shot_key(u) for u in new[field]]:
            d = diff_shots(old[field], new[field])
            if not (d["added"] or d["removed"] or d["moved"]):
                continue
            suffix = "" if field == "screenshots" else " (iPad)"
            yield ("screenshots", f"{name} changed its screenshots{suffix}", shots_summary(d),
                   {"device": label, "before": old[field], "after": new[field], "diff": d})
    if shot_key(old["icon"] or "") != shot_key(new["icon"] or ""):
        yield ("icon", f"{name} changed its icon", "New app icon",
               {"before": old["icon"], "after": new["icon"]})
    if old["name"] != new["name"]:
        yield ("title", f"{new['name']} changed its name", f"“{old['name']}” → “{new['name']}”",
               {"before": old["name"], "after": new["name"]})
    if old["price"] != new["price"]:
        yield ("price", f"{name} changed its price", f"{old['price']} → {new['price']}",
               {"before": old["price"], "after": new["price"]})
    if old["descriptionHash"] != new["descriptionHash"]:
        yield ("description", f"{name} updated its description", "Store description text changed",
               {"before": old.get("description"), "after": new.get("description")})
    if old.get("rating") and new.get("rating") and abs(new["rating"] - old["rating"]) >= RATING_THRESHOLD:
        delta = new["rating"] - old["rating"]
        verb = "rose" if delta > 0 else "dropped"
        yield ("rating", f"{name} {verb} to {new['rating']:.2f} stars",
               f"{delta:+.2f} from {old['rating']:.2f}",
               {"before": old["rating"], "after": new["rating"]})


def main():
    watch = load(os.path.join(ROOT, "apps.json"), {"apps": []})["apps"]
    events = load(EVENTS_FILE, [])
    ts = now_iso()
    day = ts[:10]
    apps_out, new_events, failures = [], [], []

    for entry in watch:
        app_id, country = parse_app(entry)
        if not app_id:
            print(f"skip: can't parse {entry}", file=sys.stderr)
            continue
        key = f"{country}-{app_id}"
        print(f"→ {key}")
        state_path = os.path.join(STATE, f"{key}.json")
        old = load(state_path, None)
        raw = fetch(app_id, country)
        if not raw:
            failures.append(key)
            if old:
                apps_out.append({**old, "lastError": ts})
            continue
        new = snapshot(raw, app_id, country)
        new["lastChecked"] = ts
        if old is None:
            new["trackedSince"] = ts
            new_events.append({
                "id": f"{day}-{key}-tracking", "type": "tracking", "app": key, "ts": ts,
                "title": f"Started tracking {new['name']}",
                "summary": f"Baseline: {len(new['screenshots'])} screenshots · v{new['version']}",
                "after": new["screenshots"],
            })
        else:
            new["trackedSince"] = old.get("trackedSince", ts)
            for etype, title, summary, extra in compare(old, new):
                suffix = "-ipad" if extra.get("device") == "iPad" else ""
                new_events.append({
                    "id": f"{day}-{key}-{etype}{suffix}", "type": etype, "app": key,
                    "ts": ts, "title": title, "summary": summary, **extra,
                })
        save(state_path, new)
        apps_out.append(new)

    # Idempotent: a re-run on the same day replaces that day's events for the same id.
    seen = {e["id"] for e in new_events}
    events = new_events + [e for e in events if e["id"] not in seen]
    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:MAX_EVENTS]
    save(EVENTS_FILE, events)

    # Feed for the panel: apps without long description text.
    slim = [{k: v for k, v in a.items() if k != "description"} for a in apps_out]
    save(FEED_FILE, {"generatedAt": ts, "apps": slim, "events": events})

    print(f"done: {len(apps_out)} apps, {len(new_events)} new events, {len(failures)} failures")
    if failures and len(failures) == len(watch):
        sys.exit(1)  # every fetch failed -> make the Action go red


if __name__ == "__main__":
    main()

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import play  # noqa: E402

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


def parse_ts(s):
    """ISO string -> aware datetime (naive values are treated as UTC)."""
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


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


PLAY_RE = re.compile(r"play\.google\.com/store/apps/details\?(?:[^#]*&)?id=([A-Za-z0-9_.]+)")


def parse_app(entry):
    """Accept {id, country, platform} or {url} entries. Returns (id, country, platform)."""
    app_id = str(entry.get("id") or "")
    country = (entry.get("country") or "").lower()
    platform = entry.get("platform") or ""
    url = entry.get("url") or ""
    if url and not app_id:
        m = PLAY_RE.search(url)
        if m:
            app_id, platform = m.group(1), "android"
            g = re.search(r"[?&]gl=([A-Za-z]{2})", url)
            country = country or (g.group(1).lower() if g else "")
        else:
            m = ID_RE.search(url)
            app_id = m.group(1) if m else ""
            c = re.search(r"apps\.apple\.com/([a-z]{2})/", url)
            country = country or (c.group(1) if c else "")
    if not platform:
        platform = "ios" if app_id.isdigit() else "android"
    return app_id, country or "us", platform


def app_key(app_id, country, platform):
    return f"gp-{country}-{app_id}" if platform == "android" else f"{country}-{app_id}"


def is_play(key):
    return key.startswith("gp-")


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


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
SERVER_DATA_RE = re.compile(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S)


UAS = [UA, ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")]


def fetch_page(app_id, country, query=""):
    """The public App Store web page. It shows the screenshot set Apple actually
    serves today (largest iPhone size), unlike the Lookup API, which often returns
    an older device-size set the developer never updated.
    Retries a few times: Apple intermittently answers CI runners with an error or a
    stripped page, and a single miss must not flip us back to the stale Lookup set."""
    fixture_dir = os.environ.get("FIXTURE_DIR")
    if fixture_dir:
        p = os.path.join(fixture_dir, f"page-{country}-{app_id}{query.replace('?', '-').replace('=', '-')}.html")
        return open(p).read() if os.path.exists(p) else None
    url = f"https://apps.apple.com/{country}/app/id{app_id}{query}"
    for attempt in range(4):
        req = urllib.request.Request(url, headers={
            "User-Agent": UAS[attempt % len(UAS)], "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                page = r.read().decode("utf-8", "replace")
            if SERVER_DATA_RE.search(page):
                return page
            print(f"  page {attempt + 1}/4: no data block ({len(page)} bytes)", file=sys.stderr)
        except Exception as e:
            print(f"  page {attempt + 1}/4 failed: {e}", file=sys.stderr)
        time.sleep(3 + attempt * 4)
    return None

def server_data(page):
    m = SERVER_DATA_RE.search(page or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _artwork_url(art, width=392):
    tpl = art.get("template") or art.get("url") or ""
    if "mzstatic" not in tpl:
        return None
    w, h = art.get("width") or 1290, art.get("height") or 2796
    size = f"{width}x{round(width * h / w)}bb.jpg"
    return re.sub(r"\{w\}x\{h\}\{c\}\.\{f\}$", size, tpl) if "{w}" in tpl else tpl


def page_screenshots(data):
    """{'phone': [...], 'pad': [...]} from shelves named product_media_phone_ / product_media_pad_."""
    out = {"phone": [], "pad": []}
    if not data:
        return out

    def walk(node, shelf=None):
        if isinstance(node, dict):
            for k, v in node.items():
                s = k if isinstance(k, str) and k.startswith("product_media_") else shelf
                if k == "screenshot" and isinstance(v, dict) and shelf:
                    url = _artwork_url(v)
                    dev = "pad" if "pad" in shelf else "phone" if "phone" in shelf else None
                    if url and dev and url not in out[dev]:
                        out[dev].append(url)
                else:
                    walk(v, s)
        elif isinstance(node, list):
            for v in node:
                walk(v, shelf)

    walk(data)
    return out


def shot_key(url):
    """mzstatic URLs end with a size segment (…/392x696bb.png). Strip it so
    the same asset served at a different size isn't seen as a new image."""
    if "googleusercontent.com" in url:
        return url.split("=")[0]
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
    varies = new.get("platform") == "android" and new["version"] == "Varies with device"
    if varies and old.get("versionDate") and old.get("versionDate") != new.get("versionDate"):
        yield ("release", f"{name} shipped an update",
               f"Updated {new['versionDate'][:10]}",
               {"before": old.get("versionDate", "")[:10], "after": new["versionDate"][:10],
                "notes": new.get("releaseNotes")})
    elif not varies and old["version"] != new["version"]:
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


def add_events(new_events):
    """Merge events into events.json. Same id on the same day replaces the old one."""
    events = load(EVENTS_FILE, [])
    seen = {e["id"] for e in new_events}
    events = new_events + [e for e in events if e["id"] not in seen]
    events.sort(key=lambda e: e["ts"], reverse=True)
    save(EVENTS_FILE, events[:MAX_EVENTS])


def app_keys():
    watch = load(os.path.join(ROOT, "apps.json"), {"apps": []})["apps"]
    keys = []
    for entry in watch:
        app_id, country, platform = parse_app(entry)
        if app_id:
            keys.append((app_key(app_id, country, platform), app_id, country))
    return keys


def rebuild_feed():
    """feed.json = what the panel loads first: slim app cards + the event log.
    Reviews, summaries and history live in their own files and load on demand."""
    apps = []
    groups = {}
    for entry in load(os.path.join(ROOT, "apps.json"), {"apps": []})["apps"]:
        i, c, pf = parse_app(entry)
        if entry.get("group"):
            groups[app_key(i, c, pf)] = entry["group"]
    for key, _, _ in app_keys():
        st = load(os.path.join(STATE, f"{key}.json"), None)
        if not st:
            continue
        slim = {k: v for k, v in st.items() if k not in ("description", "releaseNotes")}
        slim["key"] = key
        if key in groups:
            slim["group"] = groups[key]
        rv = load(os.path.join(DATA, "reviews", f"{key}.json"), None)
        if rv:
            slim["reviewStats"] = rv.get("stats")
        sm = load(os.path.join(DATA, "summaries", f"{key}.json"), None)
        if sm and (sm.get("current") or sm.get("items")):
            latest = sm.get("current") or sm["items"][0]
            slim["summaryHeadline"] = latest.get("headline")
            slim["summaryTs"] = latest.get("ts")
        apps.append(slim)
    save(FEED_FILE, {"generatedAt": now_iso(), "apps": apps, "events": load(EVENTS_FILE, [])})


def main():
    watch = load(os.path.join(ROOT, "apps.json"), {"apps": []})["apps"]
    ts = now_iso()
    day = ts[:10]
    apps_out, new_events, failures = [], [], []

    for entry in watch:
        app_id, country, platform = parse_app(entry)
        if not app_id:
            print(f"skip: can't parse {entry}", file=sys.stderr)
            continue
        key = app_key(app_id, country, platform)
        print(f"→ {key}")
        state_path = os.path.join(STATE, f"{key}.json")
        old = load(state_path, None)
        if platform == "android":
            raw = play.fetch_app(app_id, country)
        else:
            raw = fetch(app_id, country)
        if not raw:
            failures.append(key)
            if old:
                save(state_path, {**old, "lastError": ts})
                apps_out.append({**old, "lastError": ts})
            continue
        if platform == "android":
            new = play.snapshot(raw, app_id, country)
        else:
            new = snapshot(raw, app_id, country)
            new["platform"] = "ios"
            new["screenshotSource"] = "lookup"
            shots = page_screenshots(server_data(fetch_page(app_id, country)))
            if shots["phone"]:
                new["screenshots"], new["screenshotSource"] = shots["phone"], "page"
                if shots["pad"]:
                    new["ipadScreenshots"] = shots["pad"]
            elif old and old.get("screenshotSource") == "page":
                # Page unavailable today: keep yesterday's page set rather than
                # falling back to the (often stale) Lookup set.
                print("  page unavailable, keeping last page screenshots", file=sys.stderr)
                new["screenshots"] = old["screenshots"]
                new["ipadScreenshots"] = old.get("ipadScreenshots", [])
                new["screenshotSource"] = "page"
            time.sleep(1.5)  # be gentle with apps.apple.com
        new["lastChecked"] = ts
        new.pop("lastError", None)
        if old is None:
            new["trackedSince"] = ts
            new_events.append({
                "id": f"{day}-{key}-tracking", "type": "tracking", "app": key, "ts": ts,
                "title": f"Started tracking {new['name']}",
                "summary": f"Baseline: {len(new['screenshots'])} screenshots · " + (f"v{new['version']}" if new['version'] != play.VARIES else "version varies by device"),
                "after": new["screenshots"],
            })
        else:
            new["trackedSince"] = old.get("trackedSince", ts)
            resync = new["screenshotSource"] == "page" and old.get("screenshotSource") != "page"
            if resync:  # switching data source: realign silently instead of a fake "changed" event
                print(f"  screenshots resynced from {new['screenshotSource']}")
                old = {**old, "screenshots": new["screenshots"], "ipadScreenshots": new["ipadScreenshots"]}
                for e in load(EVENTS_FILE, []):
                    if e.get("app") == key and e.get("type") == "tracking":
                        e["after"] = new["screenshots"]
                        new_events.append(e)
            for etype, title, summary, extra in compare(old, new):
                suffix = "-ipad" if extra.get("device") == "iPad" else ""
                new_events.append({
                    "id": f"{day}-{key}-{etype}{suffix}", "type": etype, "app": key,
                    "ts": ts, "title": title, "summary": summary, **extra,
                })
        save(state_path, new)
        apps_out.append(new)

    add_events(new_events)
    rebuild_feed()

    print(f"done: {len(apps_out)} apps, {len(new_events)} new events, {len(failures)} failures")
    if failures and len(failures) == len(watch):
        sys.exit(1)  # every fetch failed -> make the Action go red


if __name__ == "__main__":
    main()

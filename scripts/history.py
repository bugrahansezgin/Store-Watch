#!/usr/bin/env python3
"""Version history + description history per app.

- Versions: backfilled once from the App Store web page (its "Version History"
  data), then extended every day from the Lookup API as new versions ship.
- Descriptions: every distinct description text we've seen, with timestamps.

Output: data/history/<country>-<id>.json
  {versions:[{version,date,notes,source}], descriptions:[{ts,text}], backfill:{...}}
"""
import html
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track import DATA, STATE, app_keys, load, now_iso, rebuild_feed, save  # noqa: E402

HISTORY = os.path.join(DATA, "history")
VERSION_KEYS = ("versionDisplay", "versionString", "version")
NOTES_KEYS = ("releaseNotes", "notes", "text")
DATE_KEYS = ("releaseDate", "releaseTimestamp", "date")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")


def fetch_page(app_id, country):
    fixture_dir = os.environ.get("FIXTURE_DIR")
    if fixture_dir:
        p = os.path.join(fixture_dir, f"page-{country}-{app_id}.html")
        return open(p).read() if os.path.exists(p) else None
    url = f"https://apps.apple.com/{country}/app/id{app_id}?see-all=version-history"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  page fetch failed: {e}", file=sys.stderr)
        return None


def _pick(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v)
    return None


def _walk(node, out, depth=0):
    """Find dicts that look like version-history entries anywhere in a JSON blob.
    Apple has shipped several page formats; matching on shape survives redesigns."""
    if depth > 60:
        return
    if isinstance(node, dict):
        ver, notes = _pick(node, VERSION_KEYS), _pick(node, NOTES_KEYS)
        if ver and notes is not None and re.match(r"^\d+(\.\d+){0,3}$", ver.strip()):
            out.append({"version": ver.strip(), "notes": notes,
                        "date": (_pick(node, DATE_KEYS) or "")[:25]})
        for v in node.values():
            _walk(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, depth + 1)
    elif isinstance(node, str) and len(node) > 50 and node.lstrip()[:1] in "{[":
        try:  # some pages embed JSON as strings inside JSON
            _walk(json.loads(node), out, depth + 1)
        except ValueError:
            pass


def parse_versions(page):
    found = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", page, re.S):
        body = m.group(1).strip()
        if not body or body[:1] not in "{[":
            body = html.unescape(body)
            if body[:1] not in "{[":
                continue
        try:
            _walk(json.loads(body), found)
        except ValueError:
            continue
    # Dedupe, keep the entry with the longest notes for each version
    best = {}
    for v in found:
        cur = best.get(v["version"])
        if not cur or len(v["notes"]) > len(cur["notes"]) or (not cur["date"] and v["date"]):
            best[v["version"]] = {**v, "source": "appstore"}
    return list(best.values())


def vkey(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v["version"]))


def main():
    os.makedirs(HISTORY, exist_ok=True)
    for key, app_id, country in app_keys():
        st = load(os.path.join(STATE, f"{key}.json"), None)
        if not st:
            continue
        path = os.path.join(HISTORY, f"{key}.json")
        h = load(path, {"versions": [], "descriptions": [], "backfill": None})
        versions = {v["version"]: v for v in h["versions"]}

        # One-time backfill from the web page (retried next day if it found nothing)
        bf = h.get("backfill") or {}
        if not bf.get("ok") and (not bf.get("ts") or bf["ts"][:10] < now_iso()[:10]):
            page = fetch_page(app_id, country)
            scraped = parse_versions(page) if page else []
            for v in scraped:
                versions.setdefault(v["version"], v)
            h["backfill"] = {"ok": bool(scraped), "count": len(scraped), "ts": now_iso()}
            print(f"→ {key}: backfilled {len(scraped)} versions from App Store page")

        # Current version from the daily lookup
        cur = st.get("version")
        if cur:
            entry = versions.get(cur, {})
            versions[cur] = {
                "version": cur,
                "date": entry.get("date") or (st.get("versionDate") or "")[:25],
                "notes": st.get("releaseNotes") or entry.get("notes") or "",
                "source": entry.get("source") or "tracked",
            }
        h["versions"] = sorted(versions.values(), key=vkey, reverse=True)

        # Description history
        desc = st.get("description") or ""
        if desc and (not h["descriptions"] or h["descriptions"][0]["text"] != desc):
            h["descriptions"].insert(0, {"ts": st.get("lastChecked") or now_iso(), "text": desc})

        save(path, h)
    rebuild_feed()


if __name__ == "__main__":
    main()

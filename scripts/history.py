#!/usr/bin/env python3
"""Version history + description history per app.

- Versions: backfilled once from the App Store web page (its "Version History"
  data), then extended every day from the Lookup API as new versions ship.
- Descriptions: every distinct description text we've seen, with timestamps.

Output: data/history/<country>-<id>.json
  {versions:[{version,date,notes,source}], descriptions:[{ts,text}], backfill:{...}}
"""
import datetime as dt
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track import DATA, STATE, app_keys, fetch_page, load, now_iso, rebuild_feed, save, server_data  # noqa: E402

HISTORY = os.path.join(DATA, "history")
VERSION_KEYS = ("versionDisplay", "versionString", "version")
NOTES_KEYS = ("releaseNotes", "notes", "text")
DATE_KEYS = ("releaseDate", "releaseTimestamp", "date")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")


def fetch_history_page(app_id, country):
    return fetch_page(app_id, country, "?see-all=version-history")


def _parse_date(s):
    """'Fri Jan 23 2026 09:41:40 GMT+0000 (…)' or ISO -> ISO string."""
    s = (s or "").strip()
    try:
        return dt.datetime.strptime(s[:24], "%a %b %d %Y %H:%M:%S").replace(tzinfo=dt.timezone.utc).isoformat()
    except ValueError:
        return s[:25]


def parse_titled_paragraphs(data):
    """Current App Store web format: version-history rows are TitledParagraph items
    with primarySubtitle = 'Version 1.92' / '1.92' and secondarySubtitle = date."""
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("$kind") == "TitledParagraph":
                m = re.search(r"(\d+(?:\.\d+){0,3})", n.get("primarySubtitle") or "")
                if m:
                    out.append({"version": m.group(1), "notes": n.get("text") or "",
                                "date": _parse_date(n.get("secondarySubtitle")), "source": "appstore"})
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(data)
    return out


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
            page = fetch_history_page(app_id, country)
            scraped = parse_titled_paragraphs(server_data(page)) if page else []
            if not scraped and page:
                scraped = parse_versions(page)  # older page formats
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

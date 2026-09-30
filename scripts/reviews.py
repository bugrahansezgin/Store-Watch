#!/usr/bin/env python3
"""Collect App Store customer reviews from Apple's public RSS feed.

Apple exposes the ~500 most recent reviews per app/country (10 pages x 50).
First run backfills all 10 pages; later runs stop at the first page that has
nothing new. Reviews accumulate over time, so history grows past 500.

Output: data/reviews/<country>-<id>.json  {stats, reviews:[...newest first]}
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import play  # noqa: E402
from track import parse_ts, DATA, app_keys, is_play, load, now_iso, rebuild_feed, save  # noqa: E402

REVIEWS = os.path.join(DATA, "reviews")
MAX_KEEP = 3000
MAX_PAGES = 10


def fetch_page(app_id, country, page):
    fixture_dir = os.environ.get("FIXTURE_DIR")
    if fixture_dir:
        return load(os.path.join(fixture_dir, f"reviews-{country}-{app_id}-p{page}.json"), {})
    url = (f"https://itunes.apple.com/{country}/rss/customerreviews/"
           f"page={page}/id={app_id}/sortby=mostrecent/json")
    req = urllib.request.Request(url, headers={"User-Agent": "store-watch/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            print(f"  page {page} failed ({attempt + 1}/3): {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None


def lbl(obj, *path):
    for p in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj.get("label") if isinstance(obj, dict) else obj


def parse(payload):
    entries = (payload or {}).get("feed", {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        rating = lbl(e, "im:rating")
        if rating is None:  # older feeds put the app itself first
            continue
        out.append({
            "id": lbl(e, "id"),
            "rating": int(rating),
            "title": lbl(e, "title") or "",
            "body": lbl(e, "content") or "",
            "author": lbl(e, "author", "name") or "",
            "version": lbl(e, "im:version") or "",
            "date": (lbl(e, "updated") or "")[:25],
            "votes": int(lbl(e, "im:voteSum") or 0),
        })
    return out


def stats(reviews):
    now = dt.datetime.now(dt.timezone.utc)

    def window(days):
        cutoff = now - dt.timedelta(days=days)
        rs = [r for r in reviews if r["date"] and parse_ts(r["date"]) >= cutoff]
        dist = {str(i): sum(1 for r in rs if r["rating"] == i) for i in range(1, 6)}
        avg = round(sum(r["rating"] for r in rs) / len(rs), 2) if rs else None
        return {"count": len(rs), "avg": avg, "dist": dist}

    dist_all = {str(i): sum(1 for r in reviews if r["rating"] == i) for i in range(1, 6)}
    return {
        "total": len(reviews),
        "avg": round(sum(r["rating"] for r in reviews) / len(reviews), 2) if reviews else None,
        "dist": dist_all,
        "d7": window(7),
        "d30": window(30),
        "updatedAt": now_iso(),
    }


def main():
    os.makedirs(REVIEWS, exist_ok=True)
    for key, app_id, country in app_keys():
        path = os.path.join(REVIEWS, f"{key}.json")
        store = load(path, {"reviews": []})
        known = {r["id"] for r in store["reviews"]}
        fresh = []
        if is_play(key):
            items = play.fetch_reviews(app_id, country, 200 if store["reviews"] else 500) or []
            fresh = [r for r in items if r["id"] and r["id"] not in known]
        for page in (range(1, MAX_PAGES + 1) if not is_play(key) else []):
            payload = fetch_page(app_id, country, page)
            if payload is None:
                break
            items = parse(payload)
            if not items:
                break
            new = [r for r in items if r["id"] not in known]
            fresh.extend(new)
            known.update(r["id"] for r in new)
            if known and not new and store["reviews"]:
                break  # caught up with what we already have
            time.sleep(0.5)
        merged = sorted(fresh + store["reviews"], key=lambda r: r["date"], reverse=True)[:MAX_KEEP]
        save(path, {"stats": stats(merged), "reviews": merged})
        print(f"→ {key}: +{len(fresh)} reviews ({len(merged)} stored)")
    rebuild_feed()


if __name__ == "__main__":
    main()

"""Google Play support, built on the maintained `google-play-scraper` package
(pip install google-play-scraper). Returns data in the same shape as the
App Store snapshot so the rest of the pipeline and the panel stay platform-agnostic.
"""
import datetime as dt
import hashlib
import json
import os
import sys

VARIES = "Varies with device"


def _lib():
    import google_play_scraper  # imported lazily so App Store-only runs don't need it
    return google_play_scraper


def sized(url, w=392):
    """play-lh image URLs take size params after '='."""
    if not url:
        return url
    return url.split("=")[0] + f"=w{w}"


def fetch_app(pkg, country):
    fixture_dir = os.environ.get("FIXTURE_DIR")
    if fixture_dir:
        p = os.path.join(fixture_dir, f"play-{country}-{pkg}.json")
        return json.load(open(p)) if os.path.exists(p) else None
    try:
        return _lib().app(pkg, lang="en", country=country)
    except Exception as e:
        print(f"  play fetch failed: {e}", file=sys.stderr)
        return None


def snapshot(raw, pkg, country):
    desc = raw.get("description") or ""
    updated = raw.get("updated")
    version_date = (dt.datetime.fromtimestamp(updated, dt.timezone.utc).isoformat()
                    if isinstance(updated, (int, float)) else raw.get("lastUpdatedOn") or "")
    price = raw.get("price")
    price_label = "Free" if raw.get("free", price in (0, None)) else f"{raw.get('currency') or ''} {price}".strip()
    if raw.get("offersIAP") and raw.get("inAppProductPrice"):
        price_label += f" · IAP {raw['inAppProductPrice']}"
    return {
        "id": pkg,
        "country": country,
        "platform": "android",
        "name": raw.get("title"),
        "developer": raw.get("developer"),
        "url": f"https://play.google.com/store/apps/details?id={pkg}&gl={country}",
        "icon": sized(raw.get("icon"), 512),
        "version": raw.get("version") or VARIES,
        "releaseNotes": raw.get("recentChanges") or "",
        "versionDate": version_date,
        "price": price_label,
        "rating": round(raw.get("score") or 0, 2),
        "ratingCount": raw.get("ratings"),
        "installs": raw.get("installs"),
        "genre": raw.get("genre"),
        "descriptionHash": hashlib.sha1(desc.encode()).hexdigest()[:12],
        "description": desc,
        "screenshots": [sized(u) for u in (raw.get("screenshots") or [])],
        "ipadScreenshots": [],
        "screenshotSource": "play",
    }


def fetch_reviews(pkg, country, count):
    fixture_dir = os.environ.get("FIXTURE_DIR")
    if fixture_dir:
        p = os.path.join(fixture_dir, f"play-reviews-{country}-{pkg}.json")
        items = json.load(open(p)) if os.path.exists(p) else []
    else:
        try:
            lib = _lib()
            items, _ = lib.reviews(pkg, lang="en", country=country, sort=lib.Sort.NEWEST, count=count)
        except Exception as e:
            print(f"  play reviews failed: {e}", file=sys.stderr)
            return None
    out = []
    for r in items:
        at = r.get("at")
        if isinstance(at, str):
            try:
                at = dt.datetime.fromisoformat(at)
            except ValueError:
                at = None
        if isinstance(at, dt.datetime):
            at = at.replace(tzinfo=at.tzinfo or dt.timezone.utc, microsecond=0).isoformat()
        out.append({
            "id": r.get("reviewId"),
            "rating": int(r.get("score") or 0),
            "title": "",
            "body": r.get("content") or "",
            "author": r.get("userName") or "",
            "version": r.get("reviewCreatedVersion") or "",
            "date": (at or "")[:25],
            "votes": int(r.get("thumbsUpCount") or 0),
            "reply": r.get("replyContent") or "",
        })
    return out

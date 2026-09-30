#!/usr/bin/env python3
"""Incremental AI summary of each app's reviews.

Each review is read by the model exactly once:
  - First run: the latest INITIAL_REVIEWS reviews are folded in, batch by batch.
  - Every run after that: only reviews that arrived since the last run are sent,
    together with the current summary, and the model returns the updated summary.
  - No new reviews -> no API call, zero tokens.

Provider (first one configured wins):
  ANTHROPIC_API_KEY -> Claude        (ANTHROPIC_MODEL, default claude-haiku-4-5)
  GITHUB_TOKEN      -> GitHub Models (GH_MODEL, default openai/gpt-4.1-mini) — free with Actions,
                       but capped at ~8k input tokens per request, so batches are sized to fit.

SUMMARY_FORCE=1 rebuilds every summary from scratch.
Quotes are referenced by review id and rendered from real data — the model never writes quotes.

Output: data/summaries/<key>.json
  {current:{...}, processed:[ids], items:[daily snapshots, newest first], lastError, lastRun}
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track import DATA, STATE, add_events, app_keys, load, now_iso, rebuild_feed, save  # noqa: E402
from ai import call_llm, provider  # noqa: E402

SUMMARIES = os.path.join(DATA, "summaries")
INITIAL_REVIEWS = 200          # how far back the very first summary reads
BODY_CHARS = 400               # per-review cap (keeps batches small)
KEEP_SNAPSHOTS = 60
SECTIONS = ("loves", "complaints", "requests")

SHAPE = """{
  "headline": "one sentence, max 20 words: the single most important thing users are saying",
  "sentiment": "positive" | "mixed" | "negative",
  "loves":      [{"point": "short phrase", "detail": "one sentence", "mentions": 0, "ids": ["review ids"]}],
  "complaints": [{"point": "...", "detail": "...", "mentions": 0, "ids": [...]}],
  "requests":   [{"point": "...", "detail": "...", "mentions": 0, "ids": [...]}],
  "paywall": "one sentence on what users say about pricing/subscription/paywall, or null",
  "takeaway": "one or two sentences: what a competing product team should learn or exploit",
  "delta": "one sentence: what THESE new reviews add or change vs the current summary, or null if nothing notable"
}"""

PROMPT_NEW = """You analyze {store} reviews of a competitor app for a product designer.
App: {name} ({genre}).

Reviews (id | stars | version | title — body):
{reviews}

Return ONLY a JSON object with this shape:
{shape}
Rules: max 4 items per list, ordered by mentions. "mentions" = how many of these reviews raise it.
Each item cites 1–3 ids from the list above. Be specific (features, flows, bugs, prices). English."""

PROMPT_UPDATE = """You maintain a running analysis of {store} reviews of a competitor app for a product designer.
App: {name} ({genre}).

Current summary, built from {seen} earlier reviews (JSON):
{current}

{n} NEW reviews since then (id | stars | version | title — body):
{reviews}

Update the summary with the new reviews and return ONLY the full updated JSON object with this shape:
{shape}
Rules: keep existing items unless the new reviews contradict them; add each new mention to the item's
"mentions" count; add new themes; re-order by mentions; max 4 items per list. Keep existing ids and add
1–3 ids from the new reviews where they support an item (max 3 ids per item). English."""


def line(r):
    body = (r.get("body") or "").replace("\n", " ")[:BODY_CHARS]
    title = (r.get("title") or "").replace("\n", " ")[:100]
    return f"{r['id']} | {r['rating']} | {r.get('version') or '-'} | {title} — {body}"


def batches(reviews, budget):
    out, cur, size = [], [], 0
    for r in reviews:
        ln = line(r)
        if cur and size + len(ln) > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += len(ln) + 1
    if cur:
        out.append(cur)
    return out


def clean(data, valid_ids):
    data = data or {}
    for sect in SECTIONS:
        items = []
        for it in (data.get(sect) or [])[:4]:
            if not isinstance(it, dict) or not it.get("point"):
                continue
            it["ids"] = [str(i) for i in (it.get("ids") or []) if str(i) in valid_ids][:3]
            try:
                it["mentions"] = int(it.get("mentions") or 0)
            except (TypeError, ValueError):
                it["mentions"] = 0
            items.append(it)
        data[sect] = items
    return {k: data.get(k) for k in ("headline", "sentiment", *SECTIONS, "paywall", "takeaway", "delta")}


def main():
    os.makedirs(SUMMARIES, exist_ok=True)
    force = os.environ.get("SUMMARY_FORCE") in ("1", "true", "True")
    kind, budget = provider()
    events = []
    for key, _, _ in app_keys():
        st = load(os.path.join(STATE, f"{key}.json"), None)
        rv = load(os.path.join(DATA, "reviews", f"{key}.json"), None)
        if not st or not rv or not rv.get("reviews"):
            continue
        path = os.path.join(SUMMARIES, f"{key}.json")
        store = load(path, {})
        if force or "processed" not in store:  # first run, or old weekly format
            store = {"items": store.get("items", []) if not force else [], "processed": [], "current": None}
        processed = set(store["processed"])
        all_ids = {r["id"] for r in rv["reviews"]}

        # Oldest first so the summary evolves in time order
        pending = [r for r in rv["reviews"] if r["id"] not in processed]
        if not store["current"]:
            pending = pending[:INITIAL_REVIEWS]
        pending.reverse()
        if not pending:
            print(f"→ {key}: no new reviews, skipping")
            continue
        if not kind:
            store["lastError"] = {"ts": now_iso(), "message": "No AI provider configured"}
            save(path, store)
            continue

        ctx = {"store": "Google Play" if st.get("platform") == "android" else "App Store",
               "name": st["name"], "genre": st.get("genre") or "", "shape": SHAPE}
        current, model, done, err = store["current"], None, [], None
        for batch in batches(pending, budget):
            text = "\n".join(line(r) for r in batch)
            if current:
                slim = {k: v for k, v in current.items() if k != "delta"}
                prompt = PROMPT_UPDATE.format(**ctx, seen=len(processed) + len(done), n=len(batch),
                                              current=json.dumps(slim, ensure_ascii=False), reviews=text)
            else:
                prompt = PROMPT_NEW.format(**ctx, reviews=text)
            try:
                data, model = call_llm(prompt)
            except Exception as e:
                err = str(e)
                print(f"→ {key}: summary failed: {err}", file=sys.stderr)
                break
            current = clean(data, all_ids)
            done.extend(r["id"] for r in batch)
            time.sleep(4 if kind == "github" else 1)  # stay under per-minute limits

        if done:
            ts = now_iso()
            seen = [r for r in rv["reviews"] if r["id"] in processed or r["id"] in set(done)]
            avg = round(sum(r["rating"] for r in seen) / len(seen), 2)
            dist = {str(i): sum(1 for r in seen if r["rating"] == i) for i in range(1, 6)}
            new_avg = round(sum(r["rating"] for r in pending if r["id"] in set(done)) / len(done), 2)
            current.update({"ts": ts, "model": model, "count": len(seen), "avg": avg, "dist": dist,
                            "newCount": len(done), "newAvg": new_avg,
                            "since": min((r["date"] for r in seen if r["date"]), default="")})
            store["current"] = current
            store["processed"] = sorted(processed | set(done))
            # one snapshot per day (same-day re-runs overwrite)
            items = [i for i in store.get("items", []) if i.get("ts", "")[:10] != ts[:10]]
            store["items"] = [dict(current)] + items[:KEEP_SNAPSHOTS - 1]
            first = not processed
            events.append({
                "id": f"{ts[:10]}-{key}-reviews", "type": "reviews", "app": key, "ts": ts,
                "title": f"{st['name']}: {'review summary' if first else 'new reviews analyzed'}",
                "summary": (current.get("headline") if first else (current.get("delta") or current.get("headline")) or "")
                + f" · {len(done)} {'reviews' if first else 'new'}, {new_avg}★"})
            print(f"→ {key}: folded in {len(done)} reviews with {model}")
        store["lastError"] = {"ts": now_iso(), "message": err[:400]} if err else None
        store["lastRun"] = now_iso()
        save(path, store)
    if events:
        add_events(events)
    rebuild_feed()


if __name__ == "__main__":
    main()

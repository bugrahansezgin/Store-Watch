#!/usr/bin/env python3
"""Weekly AI summary of each app's recent reviews.

Provider (first one configured wins):
  ANTHROPIC_API_KEY  -> Claude  (model: ANTHROPIC_MODEL, default claude-haiku-4-5)
  GITHUB_TOKEN       -> GitHub Models, free with Actions (model: GH_MODEL, default openai/gpt-4.1-mini)

Runs at most once every 6 days per app (SUMMARY_FORCE=1 to override).
Quotes are referenced by review id and rendered from real data — the model
never writes the quotes itself.

Output: data/summaries/<country>-<id>.json  {items:[newest first]}
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track import parse_ts, DATA, STATE, add_events, app_keys, load, now_iso, rebuild_feed, save  # noqa: E402

SUMMARIES = os.path.join(DATA, "summaries")
MIN_GAP_DAYS = 6
MAX_REVIEWS = 150
KEEP = 52

PROMPT = """You analyze {store} reviews of a competitor app for a product designer.
App: {name} ({genre}). Window: {window}. {n} reviews, average {avg}★.

Reviews (id | stars | version | title — body):
{reviews}

Return ONLY a JSON object, no prose, with this shape:
{{
  "headline": "one sentence, max 20 words: the single most important thing users are saying",
  "sentiment": "positive" | "mixed" | "negative",
  "loves": [{{"point": "short phrase", "detail": "one sentence", "ids": ["review ids"]}}],
  "complaints": [{{"point": "...", "detail": "...", "ids": [...]}}],
  "requests": [{{"point": "...", "detail": "...", "ids": [...]}}],
  "paywall": "one sentence on what users say about pricing/subscription/paywall, or null",
  "takeaway": "one or two sentences: what a competing product team should learn or exploit"
}}
Rules: max 4 items per list, ordered by how often it comes up. Each item cites 1–3 real ids from above.
Be specific (name features, flows, bugs). Write in English."""


def recent_reviews(reviews, days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return [r for r in reviews if r["date"] and parse_ts(r["date"]) >= cutoff]


def call_llm(prompt):
    if os.environ.get("FAKE_LLM"):
        return json.loads(os.environ["FAKE_LLM"]), "fake"
    if os.environ.get("ANTHROPIC_API_KEY"):
        model = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5"
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 1500,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            text = "".join(b.get("text", "") for b in json.load(r)["content"])
    elif os.environ.get("GITHUB_TOKEN"):
        model = os.environ.get("GH_MODEL") or "openai/gpt-4.1-mini"
        req = urllib.request.Request(
            "https://models.github.ai/inference/chat/completions",
            data=json.dumps({"model": model, "temperature": 0.2,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            text = json.load(r)["choices"][0]["message"]["content"]
    else:
        raise RuntimeError("No ANTHROPIC_API_KEY or GITHUB_TOKEN set")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)), model


def main():
    os.makedirs(SUMMARIES, exist_ok=True)
    force = os.environ.get("SUMMARY_FORCE") in ("1", "true", "True")
    events = []
    for key, _, _ in app_keys():
        st = load(os.path.join(STATE, f"{key}.json"), None)
        rv = load(os.path.join(DATA, "reviews", f"{key}.json"), None)
        if not st or not rv or not rv.get("reviews"):
            continue
        path = os.path.join(SUMMARIES, f"{key}.json")
        store = load(path, {"items": []})
        last = store["items"][0]["ts"] if store["items"] else None
        if last and not force:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)
            if age.days < MIN_GAP_DAYS:
                print(f"→ {key}: summary is {age.days}d old, skipping")
                continue

        # Prefer the last 7 days; widen to 30, then to the latest N, if it's quiet
        window, pool = "last 7 days", recent_reviews(rv["reviews"], 7)
        if len(pool) < 15:
            window, pool = "last 30 days", recent_reviews(rv["reviews"], 30)
        if len(pool) < 15:
            window, pool = f"latest {min(len(rv['reviews']), MAX_REVIEWS)} reviews", rv["reviews"]
        pool = pool[:MAX_REVIEWS]
        avg = round(sum(r["rating"] for r in pool) / len(pool), 2)
        lines = "\n".join(
            f"{r['id']} | {r['rating']} | {r['version']} | {r['title'][:120]} — {r['body'][:600]}".replace("\n", " ")
            for r in pool)
        prompt = PROMPT.format(store="Google Play" if st.get("platform") == "android" else "App Store", name=st["name"], genre=st.get("genre"), window=window,
                               n=len(pool), avg=avg, reviews=lines)
        try:
            data, model = call_llm(prompt)
        except Exception as e:
            print(f"→ {key}: summary failed: {e}", file=sys.stderr)
            continue

        valid = {r["id"] for r in pool}
        for sect in ("loves", "complaints", "requests"):
            items = data.get(sect) or []
            for it in items:
                it["ids"] = [i for i in (it.get("ids") or []) if str(i) in valid][:3]
            data[sect] = items[:4]
        ts = now_iso()
        dist = {str(i): sum(1 for r in pool if r["rating"] == i) for i in range(1, 6)}
        item = {"ts": ts, "model": model, "window": window, "count": len(pool), "avg": avg,
                "dist": dist, **{k: data.get(k) for k in
                                 ("headline", "sentiment", "loves", "complaints", "requests", "paywall", "takeaway")}}
        store["items"] = [item] + store["items"][:KEEP - 1]
        save(path, store)
        events.append({"id": f"{ts[:10]}-{key}-reviews", "type": "reviews", "app": key, "ts": ts,
                       "title": f"{st['name']}: review summary",
                       "summary": f"{item['headline'] or ''} · {len(pool)} reviews, {avg}★ ({window})"})
        print(f"→ {key}: summarized {len(pool)} reviews with {model}")
    if events:
        add_events(events)
    rebuild_feed()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""AI read of each app's release history: what was added, changed, removed, fixed.

Evidence per version (never invented):
  - release notes
  - store description changes around that release (when we tracked them)
  - screenshot / name / icon / price changes detected around that release

Each version is analyzed once and cached. Trivial notes ("bug fixes") are tagged
as maintenance without an API call. The product-direction overview is rebuilt only
when at least one new version was analyzed.

Output: data/versions_ai/<key>.json
  {versions:{"1.2.3":{summary, added[], changed[], removed[], fixed[], ts}},
   overview:{headline, themes[], removed[], ts, model, basedOn}, lastError}
"""
import datetime as dt
import difflib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai import call_llm, provider  # noqa: E402
from track import DATA, EVENTS_FILE, app_keys, load, now_iso, parse_ts, rebuild_feed, save  # noqa: E402

OUT = os.path.join(DATA, "versions_ai")
MAX_BACKFILL = 30      # how many past versions the first run reads
OVERVIEW_OF = 20       # how many recent versions feed the overview
LISTS = ("added", "changed", "removed", "fixed")
TRIVIAL = re.compile(
    r"^\W*(bug\s*fix(es)?|minor\s+(bug\s+)?fix(es)?|performance\s+improvements?|stability\s+improvements?|"
    r"improvements?|small\s+(fixes|tweaks)|various\s+(bug\s+)?fixes|general\s+improvements?|"
    r"thanks\s+for\s+using.*|we\s+update\s+the\s+app\s+regularly.*)[\W\s]*"
    r"(and\s+(bug\s*fix(es)?|performance\s+improvements?|stability\s+improvements?))?[\W\s]*$",
    re.I)

PROMPT_VERSIONS = """You analyze the release history of a competitor app ({store}) for a product designer.
App: {name} ({genre}).

For each version below, say what was ADDED, CHANGED, REMOVED and FIXED — using ONLY the evidence given
(release notes, store-description diffs, listing changes). Do not guess. If the evidence is vague, keep lists short.
Removed includes features, content or claims that disappeared from the description.

{blocks}

Return ONLY JSON:
{{"versions":[{{"version":"x.y","summary":"one sentence, max 18 words","added":["short phrase"],"changed":[],"removed":[],"fixed":[]}}]}}
Max 4 items per list. English."""

PROMPT_OVERVIEW = """Below are per-version summaries of a competitor app's recent releases ({store}), newest first.
App: {name} ({genre}). Release cadence: {cadence}.

{items}

Return ONLY JSON:
{{"headline":"one sentence, max 22 words: where this product is heading",
  "themes":[{{"point":"short phrase","detail":"one sentence","versions":["x.y"]}}],
  "removed":["things removed, deprecated or de-emphasized over this period"],
  "takeaway":"one or two sentences: what a competing product team should take from this"}}
Max 4 themes, ordered by importance. English."""


def is_trivial(notes):
    n = (notes or "").strip()
    return not n or len(n) < 12 or bool(TRIVIAL.match(n))


def desc_diff(descs, start, end):
    """Unified diff of the store description between two dates, if we tracked both sides."""
    if not descs or not end:
        return ""
    ordered = sorted(descs, key=lambda d: d["ts"])
    before = [d for d in ordered if start and d["ts"] <= start]
    after = [d for d in ordered if d["ts"] <= end]
    if not before or not after or before[-1]["text"] == after[-1]["text"]:
        return ""
    diff = difflib.unified_diff(before[-1]["text"].splitlines(), after[-1]["text"].splitlines(), lineterm="", n=0)
    lines = [ln for ln in diff if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return "\n".join(lines)[:1500]


def listing_changes(events, key, start, end):
    out = []
    for e in events:
        if e.get("app") != key or e.get("type") not in ("screenshots", "title", "icon", "price"):
            continue
        if (not start or e["ts"] > start) and e["ts"] <= end:
            out.append(f"{e['type']}: {e.get('summary', '')}")
    return out


def cadence(versions):
    dates = sorted(parse_ts(v["date"]) for v in versions if v.get("date"))
    if len(dates) < 2:
        return {"text": "unknown"}
    now = dt.datetime.now(dt.timezone.utc)
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days >= 0]
    recent = sorted(gaps[-10:])
    median = recent[len(recent) // 2] if recent else None
    last90 = sum(1 for d in dates if (now - d).days <= 90)
    text = f"{last90} releases in the last 90 days" + (f", typically every {median} days" if median is not None else "")
    return {"text": text, "last90": last90, "medianDays": median, "lastRelease": dates[-1].isoformat()}


def main():
    os.makedirs(OUT, exist_ok=True)
    force = os.environ.get("SUMMARY_FORCE") in ("1", "true", "True")
    kind, budget = provider()
    events = load(EVENTS_FILE, [])
    touched_events = False
    for key, _, _ in app_keys():
        st = load(os.path.join(DATA, "state", f"{key}.json"), None)
        h = load(os.path.join(DATA, "history", f"{key}.json"), None)
        if not st or not h or not h.get("versions"):
            continue
        path = os.path.join(OUT, f"{key}.json")
        store = {} if force else load(path, {})
        store.setdefault("versions", {})
        store["cadence"] = cadence(h["versions"])
        known = store["versions"]
        ordered = h["versions"]  # newest first
        first_run = not known
        todo = [v for v in ordered if v["version"] not in known][: MAX_BACKFILL if first_run else 50]
        ctx = {"store": "Google Play" if st.get("platform") == "android" else "App Store",
               "name": st["name"], "genre": st.get("genre") or ""}

        # Build evidence blocks; trivial releases get tagged without an API call
        blocks, err, new_done = [], None, 0
        for v in todo:
            idx = ordered.index(v)
            prev = ordered[idx + 1] if idx + 1 < len(ordered) else None
            start = prev.get("date") if prev else None
            end_dt = parse_ts(v["date"]) + dt.timedelta(days=2) if v.get("date") else None
            end = end_dt.isoformat() if end_dt else now_iso()
            dd = desc_diff(h.get("descriptions"), start, end)
            lc = listing_changes(events, key, start, end)
            if is_trivial(v.get("notes")) and not dd and not lc:
                known[v["version"]] = {"summary": "Maintenance release (bug fixes / performance).",
                                       "trivial": True, "ts": now_iso(), **{k: [] for k in LISTS}}
                continue
            b = f"### v{v['version']} ({(v.get('date') or '')[:10]})\nRelease notes:\n{(v.get('notes') or '(none)')[:1200]}"
            if dd:
                b += f"\nStore description diff (- removed, + added):\n{dd}"
            if lc:
                b += "\nListing changes: " + "; ".join(lc)[:500]
            blocks.append((v["version"], b))

        if blocks and not kind:
            err = "No AI provider configured"
        # Batch blocks to fit the provider's request budget
        batches, cur, size = [], [], 0
        for ver, b in blocks:
            if cur and size + len(b) > budget:
                batches.append(cur); cur, size = [], 0
            cur.append((ver, b)); size += len(b)
        if cur:
            batches.append(cur)
        for batch in ([] if err else batches):
            try:
                data, model = call_llm(PROMPT_VERSIONS.format(**ctx, blocks="\n\n".join(x[1] for x in batch)))
            except Exception as e:
                err = str(e)[:400]
                print(f"→ {key}: version analysis failed: {err}", file=sys.stderr)
                break
            wanted = {x[0] for x in batch}
            for item in data.get("versions") or []:
                ver_ = str(item.get("version", "")).lstrip("vV")
                if ver_ in wanted:
                    known[ver_] = {"summary": item.get("summary") or "", "ts": now_iso(), "model": model,
                                   **{k: [str(x) for x in (item.get(k) or [])][:4] for k in LISTS}}
                    new_done += 1
            time.sleep(4 if kind == "github" else 1)

        # Overview only when something new was analyzed (or it's missing)
        need_overview = (new_done or not store.get("overview")) and not err and kind
        analyzed = [(v["version"], known[v["version"]]) for v in ordered if v["version"] in known][:OVERVIEW_OF]
        if need_overview and any(not a.get("trivial") for _, a in analyzed):
            lines = []
            for ver, a in analyzed:
                parts = [f"v{ver}: {a['summary']}"] + [f"{k}: {', '.join(a[k])}" for k in LISTS if a.get(k)]
                lines.append(" | ".join(parts))
            try:
                data, model = call_llm(PROMPT_OVERVIEW.format(**ctx, cadence=store["cadence"]["text"],
                                                             items="\n".join(lines)[:budget]))
                store["overview"] = {
                    "headline": data.get("headline"), "takeaway": data.get("takeaway"),
                    "themes": [t for t in (data.get("themes") or []) if isinstance(t, dict)][:4],
                    "removed": [str(x) for x in (data.get("removed") or [])][:6],
                    "ts": now_iso(), "model": model, "basedOn": [v for v, _ in analyzed]}
            except Exception as e:
                err = str(e)[:400]

        # Attach the AI read to matching release events in the activity feed
        for e in events:
            if e.get("app") == key and e.get("type") == "release":
                ver = str(e.get("after") or "")
                if ver in known and not known[ver].get("trivial") and e.get("ai") != known[ver]["summary"]:
                    e["ai"] = known[ver]["summary"]
                    touched_events = True

        store["lastError"] = {"ts": now_iso(), "message": err} if err else None
        store["lastRun"] = now_iso()
        save(path, store)
        print(f"→ {key}: {new_done} versions analyzed, {len(todo) - len(blocks)} maintenance")
    if touched_events:
        save(EVENTS_FILE, events)
    rebuild_feed()


if __name__ == "__main__":
    main()

"""Shared LLM client for Store Watch.

Provider (first one configured wins):
  ANTHROPIC_API_KEY -> Claude        (ANTHROPIC_MODEL, default claude-haiku-4-5)
  GITHUB_TOKEN      -> GitHub Models (GH_MODEL, default openai/gpt-4.1-mini), free with Actions,
                       ~8k input tokens per request — callers size prompts with provider()[1].
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


def provider():
    if os.environ.get("FAKE_LLM"):
        return "fake", 60000
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", 60000
    if os.environ.get("GITHUB_TOKEN"):
        return "github", 16000   # chars of review text per request (~4k tokens) to stay under 8k input
    return None, 0


def _post(url, body, headers):
    """POST JSON, return (status, parsed_json). Raises with the raw response on failure."""
    for attempt in range(4):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read().decode("utf-8", "replace"), e.code
            if status == 429 and attempt < 3:
                wait = int(e.headers.get("retry-after") or 20 * (attempt + 1))
                print(f"  rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(min(wait, 120))
                continue
            raise RuntimeError(f"HTTP {status} from {url.split('/')[2]}: {raw[:300] or '(empty body)'}")
        try:
            return status, json.loads(raw)
        except ValueError:
            raise RuntimeError(f"HTTP {status} from {url.split('/')[2]}, not JSON: {raw[:300] or '(empty body)'}")
    raise RuntimeError("rate limited too many times")


def call_llm(prompt):
    kind, _ = provider()
    if kind == "fake":
        return json.loads(os.environ["FAKE_LLM"]), "fake"
    if kind == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5"
        _, resp = _post("https://api.anthropic.com/v1/messages",
                        {"model": model, "max_tokens": 1800, "messages": [{"role": "user", "content": prompt}]},
                        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
        text = "".join(b.get("text", "") for b in resp.get("content", []))
    elif kind == "github":
        headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                   "Content-Type": "application/json", "Accept": "application/json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        # Current endpoint first, then the older Azure-hosted one (same token, same free tier)
        endpoints = [("https://models.github.ai/inference/chat/completions",
                      os.environ.get("GH_MODEL") or "openai/gpt-4.1-mini"),
                     ("https://models.inference.ai.azure.com/chat/completions", "gpt-4o-mini")]
        errors, resp = [], None
        for url, model in endpoints:
            try:
                _, resp = _post(url, {"model": model, "temperature": 0.2, "max_tokens": 1800,
                                      "messages": [{"role": "user", "content": prompt}]}, headers)
                if resp.get("choices"):
                    break
                errors.append(f"{model}: no choices in {str(resp)[:200]}")
                resp = None
            except RuntimeError as e:
                errors.append(f"{model}: {e}")
                print(f"  {model} failed: {e}", file=sys.stderr)
        if not resp:
            raise RuntimeError(" | ".join(errors))
        text = resp["choices"][0]["message"].get("content") or ""
    else:
        raise RuntimeError("No AI provider: add ANTHROPIC_API_KEY, or make sure the workflow has "
                           "'permissions: models: read' and passes GITHUB_TOKEN")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"model returned no JSON: {text[:200] or '(empty)'}")
    try:
        return json.loads(m.group(0)), model
    except ValueError as e:
        raise RuntimeError(f"model JSON didn't parse ({e}): {m.group(0)[:200]}")



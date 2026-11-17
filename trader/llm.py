"""Ollama /api/chat client for the scripted callers (dashboard chat, tools/review.py) when
config.llm_backend(caller) == "ollama". Primary host first (OLLAMA_URL / the caller's model), then
the fallback host (OLLAMA_FALLBACK_URL / OLLAMA_FALLBACK_MODEL) on a connection error or timeout,
the same rule tools/news.py applies to digests. Nothing here touches the database."""
from __future__ import annotations

import re
import sys
import time

import requests

from trader import config

DEFAULT_TIMEOUT = 180.0
TEMPERATURE = 0.1
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class LLMError(Exception):
    """No Ollama host could answer (message is safe to show the user)."""


def strip_think(text: str | None) -> str:
    """Drop <think>...</think> blocks (and an unterminated leading one)."""
    t = _THINK.sub("", text or "")
    if t.lstrip().startswith("<think>"):
        t = ""  # the model ran out of tokens while thinking
    return t.strip()


def _post(url: str, body: dict, timeout: float) -> dict:
    endpoint = f"{url.rstrip('/')}/api/chat"
    r = requests.post(endpoint, json=body, timeout=timeout)
    if r.status_code == 400 and "think" in body:
        # a model without thinking support rejects the flag: retry once without it
        body = {k: v for k, v in body.items() if k != "think"}
        r = requests.post(endpoint, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ollama_chat(messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
                format: dict | str | None = None, timeout: float = DEFAULT_TIMEOUT,
                fallback: bool = True) -> dict:
    """One non-streaming chat request. -> {"message": {...content stripped of <think>...},
    "model", "url", "prompt_tokens", "eval_tokens", "duration_ms"}. Raises LLMError when neither
    host answers; HTTP errors other than that propagate as requests.HTTPError wrapped in LLMError too."""
    hosts = [(config.OLLAMA_URL, model or config.OLLAMA_MODEL)]
    if fallback and config.OLLAMA_FALLBACK_URL:
        hosts.append((config.OLLAMA_FALLBACK_URL, config.OLLAMA_FALLBACK_MODEL))
    errors = []
    for url, mdl in hosts:
        body = {"model": mdl, "messages": messages, "stream": False, "think": False,
                "options": {"temperature": TEMPERATURE, "num_ctx": config.OLLAMA_NUM_CTX}}
        if tools:
            body["tools"] = tools
        if format is not None:
            body["format"] = format
        t0 = time.monotonic()
        try:
            d = _post(url, body, timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"[llm] {mdl}@{url} unavailable ({type(e).__name__})"
                  + ("; falling back" if (url, mdl) != hosts[-1] else ""), file=sys.stderr)
            errors.append(f"{url}: {type(e).__name__}")
            continue
        except (requests.RequestException, ValueError) as e:
            raise LLMError(f"Ollama at {url} ({mdl}) failed: {e}") from e
        msg = dict(d.get("message") or {})
        msg["content"] = strip_think(msg.get("content"))
        msg.pop("thinking", None)
        return {"message": msg, "model": mdl, "url": url,
                "prompt_tokens": int(d.get("prompt_eval_count") or 0), "eval_tokens": int(d.get("eval_count") or 0),
                "duration_ms": int((time.monotonic() - t0) * 1000)}
    raise LLMError("Ollama is not reachable (" + "; ".join(errors) + "). Check that the Ollama service is running.")


def usage_doc(model: str, prompt_tokens: int, eval_tokens: int, duration_ms: int | None = None,
              turns: int = 1) -> dict:
    """A `claude -p`-shaped result document so web.chat.record_usage can store an Ollama call in
    claude_usage: model "ollama:<model>", cost 0 (local). Nothing is stored when both token counts
    are 0 (record_usage's 'nothing reported' rule)."""
    name = f"ollama:{model}"
    return {"usage": {"input_tokens": prompt_tokens, "output_tokens": eval_tokens}, "total_cost_usd": 0,
            "modelUsage": {name: {"costUSD": 0}}, "num_turns": turns, "duration_ms": duration_ms}

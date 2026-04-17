"""Shared OpenAI-compatible API helper.

Two model env-var sets:
    ACTIVITY_LOG_API_BASE / API_KEY / MODEL          → small fast model (per-turn AI fields)
    ACTIVITY_LOG_SYNTH_API_BASE / API_KEY / MODEL    → large model (long-log synthesis)
        Falls back to the small set if any synth var is unset.

Returns None on missing config, network error, or bad response (fail-open).
"""
import json
import os
import sys
import urllib.error
import urllib.request

_TIMEOUT = 60


def _log_err(msg):
    """Print errors to stderr. Always on — per-turn hook stderr is invisible
    unless the hook itself fails, so no noise in normal use."""
    print(f"[_api] {msg}", file=sys.stderr)


def _env(prefix, key):
    return os.environ.get(f"{prefix}_{key}", "")


def _resolve_var(synth, key):
    """Per-var fallback: SYNTH_* if set, else ACTIVITY_LOG_*.

    Lets the user override only the fields they need (e.g. MODEL) while
    sharing API_BASE / API_KEY across both sets.
    """
    if synth:
        v = _env("ACTIVITY_LOG_SYNTH", key)
        if v:
            return v
    return _env("ACTIVITY_LOG", key)


def configured(synth=False):
    return all(_resolve_var(synth, k) for k in ("API_BASE", "API_KEY", "MODEL"))


def call_llm(system, user_msg, max_tokens=512, temperature=0.2, synth=False, timeout=None):
    """POST to an OpenAI-compatible chat/completions endpoint.

    Returns assistant message text or None on any failure.
    """
    base = _resolve_var(synth, "API_BASE").rstrip("/")
    key = _resolve_var(synth, "API_KEY")
    model = _resolve_var(synth, "MODEL")
    if not (base and key and model):
        return None

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT) as resp:
            raw = resp.read()
            result = json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        _log_err(f"HTTP {e.code} {e.reason} from {base}/chat/completions | body={body}")
        return None
    except urllib.error.URLError as e:
        _log_err(f"URLError reaching {base}/chat/completions: {e.reason}")
        return None
    except Exception as e:
        _log_err(f"{type(e).__name__}: {e}")
        return None

    try:
        return result["choices"][0]["message"]["content"].strip() or None
    except (KeyError, IndexError):
        _log_err(f"unexpected response shape: {str(result)[:300]}")
        return None

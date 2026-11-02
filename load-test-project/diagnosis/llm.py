"""
llm.py — one place that talks to Ollama.

Why this exists: with qwen3 the old calls could sit for minutes with no sign of life, return
"<think>...</think>" text that confused the confidence parser, and on any error the whole run
died with nothing to show. This module
  * asks Ollama not to "think" (think=false) and strips any <think> block that still arrives,
  * sets a temperature of 0 so the same facts give (nearly) the same wording,
  * fails with a clear LLMError (model missing, Ollama not running, timeout) that the pipeline
    turns into a labelled fallback instead of an empty screen.
"""
import re
import time

import requests

OLLAMA_URL = "http://localhost:11434"
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """The model could not be used. The message is safe to show to the user."""


def ollama_status(url: str = OLLAMA_URL, timeout: float = 2.0) -> dict:
    """{'running': bool, 'models': [names], 'error': str|None} — used by /api/models and the preflight."""
    try:
        r = requests.get(f"{url}/api/tags", timeout=timeout)
        r.raise_for_status()
        names = sorted(m.get("name", "") for m in r.json().get("models", []))
        return {"running": True, "models": names, "error": None}
    except Exception as e:  # connection refused, timeout, bad json
        return {"running": False, "models": [], "error": f"{type(e).__name__}: {e}"}


def preflight(model: str, url: str = OLLAMA_URL) -> None:
    st = ollama_status(url)
    if not st["running"]:
        raise LLMError("Ollama is not running on this machine. Start it (open the Ollama app or run `ollama serve`).")
    base = model.split(":")[0]
    if model not in st["models"] and not any(m.split(":")[0] == base and ":" not in model for m in st["models"]):
        have = ", ".join(st["models"]) or "none"
        raise LLMError(f"Model '{model}' is not installed. Run `ollama pull {model}`. Installed models: {have}.")


def clean(text: str) -> str:
    return THINK_RE.sub("", text or "").strip()


def chat(model: str, system: str, user: str, url: str = OLLAMA_URL, timeout: int = 240,
         max_tokens: int = 700) -> str:
    t0 = time.time()
    try:
        r = requests.post(
            f"{url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "stream": False,
                "think": False,                      # qwen3: skip the slow hidden reasoning
                "keep_alive": "20m",                 # keep the model in memory between calls
                "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": 8192},
            },
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        raise LLMError("Could not reach Ollama at " + url + ". Is it running?")
    except requests.exceptions.Timeout:
        raise LLMError(f"The model did not answer within {timeout}s (on CPU a {model} reply can be slow). "
                       "Try a smaller model, or use the built-in summary.")
    if r.status_code == 404:
        raise LLMError(f"Model '{model}' is not installed. Run `ollama pull {model}`.")
    if r.status_code >= 400:
        raise LLMError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
    text = clean(r.json().get("message", {}).get("content", ""))
    if not text:
        raise LLMError("The model returned an empty answer.")
    return text
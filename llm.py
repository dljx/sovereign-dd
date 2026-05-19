"""LLM wrapper â€" Gemma 4 31B, key rotation, auto model-ID detection, grounding, JSON extraction."""

import asyncio
import json
import os
import random
import re
import threading
import time
from itertools import cycle

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

_raw_keys = os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", ""))
_keys = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if not _keys:
    raise RuntimeError("No GEMINI_API_KEYS found in .env")

_key_cycle = cycle(_keys)
_key_lock  = threading.Lock()       # thread-safe rotation for concurrent async calls
_clients: dict[str, genai.Client] = {}
_model_ids: dict[str, str] = {}     # key -> verified model ID string

_key_cooldowns: dict[str, float] = {}   # key -> unix timestamp when available again
_cooldown_lock = threading.Lock()


def _client() -> tuple[genai.Client, str]:
    """Return (client, api_key) for the next key in rotation. Thread-safe."""
    with _key_lock:
        key = next(_key_cycle)
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key], key


def _client_for(key: str) -> genai.Client:
    """Return (or create) a client for a specific key."""
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def _pick_key() -> tuple[str, float]:
    """Return (key, wait_secs) for the soonest-available key."""
    now = time.time()
    with _cooldown_lock:
        best = min(_keys, key=lambda k: _key_cooldowns.get(k, 0.0))
        wait = max(0.0, _key_cooldowns.get(best, 0.0) - now)
        return best, wait


def _cool_key(key: str, duration: float) -> None:
    """Mark a key as rate-limited for duration seconds."""
    with _cooldown_lock:
        _key_cooldowns[key] = time.time() + duration
    print(f"  [llm] key ...{key[-6:]} cooling for {duration:.0f}s")


def _resolve_model(client: genai.Client, key: str, model: str) -> str:
    """Return the working model ID string for this key (cached after first call)."""
    if key in _model_ids:
        return _model_ids[key]
    for candidate in [model, f"models/{model}"]:
        try:
            client.models.generate_content(
                model=candidate,
                contents="hi",
                config=types.GenerateContentConfig(max_output_tokens=1),
            )
            _model_ids[key] = candidate
            return candidate
        except Exception:
            continue
    _model_ids[key] = model
    return model


def _jittered(base: float) -> float:
    """Add Â±25% jitter to a backoff delay to desynchronise concurrent retries."""
    return base * (0.75 + random.random() * 0.5)


def call_gemini(
    system: str,
    user: str,
    model: str = "gemma-4-31b-it",
    temperature: float = 0.3,
    max_retries: int = 12,
    grounding: bool = False,
    api_key: str | None = None,
) -> str:
    """Call the model and return raw text. Retries on 429/500/503. Thread-safe key rotation.

    Backoff caps at 120 s (with Â±25% jitter) so the retry window covers ~15 min
    of API-wide instability â€" enough to outlast most Gemma outages.
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            if api_key is not None:
                client = _client_for(api_key)
                key = api_key
            else:
                client, key = _client()
            resolved = _resolve_model(client, key, model)

            config_kwargs: dict = dict(
                system_instruction=system,
                temperature=temperature,
                max_output_tokens=8192,
            )
            if grounding:
                config_kwargs["tools"] = [{"google_search": {}}]

            response = client.models.generate_content(
                model=resolved,
                contents=user,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return response.text or ""
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if "429" in err_str or "quota" in err_str or "503" in err_str:
                wait = _jittered(min(2 ** (attempt + 1), 30))
                print(f"  [llm] rate limit on attempt {attempt + 1}, retrying in {wait:.0f}s...")
                time.sleep(wait)
            elif "500" in err_str or "502" in err_str:
                if attempt < max_retries - 1:
                    wait = _jittered(min(2 ** (attempt + 1), 30))
                    print(f"  [llm] server error on attempt {attempt + 1}, retrying in {wait:.0f}s...")
                    time.sleep(wait)
                else:
                    raise
            else:
                raise
    raise RuntimeError(f"LLM failed after {max_retries} attempts: {last_err}")


_api_semaphore: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    """Lazy-init semaphore sized to number of API keys (max concurrent calls = num keys)."""
    global _api_semaphore
    if _api_semaphore is None:
        _api_semaphore = asyncio.Semaphore(len(_keys))
    return _api_semaphore


async def call_gemini_async(
    system: str,
    user: str,
    model: str = "gemma-4-31b-it",
    temperature: float = 0.3,
    max_retries: int = 12,
    grounding: bool = False,
) -> str:
    """Async wrapper with per-key cooldown tracking to prevent thundering herd.

    On each attempt:
      1. _pick_key() selects the soonest-available key. If all keys are cooling,
         sleeps until the best one is ready, plus a small per-caller jitter to
         spread concurrent waiters.
      2. Acquires the global semaphore only for the duration of the actual call,
         so sleeping retries never hold a slot.
      3. On 429: marks that specific key as cooling so other callers route around it.
         On 5xx: brief sleep without cooling the key (not a quota issue).
    """
    last_err = None
    for attempt in range(max_retries):
        key, wait = _pick_key()
        if wait > 0:
            wait += random.uniform(0, 2)  # spread concurrent waiters
            print(f"  [llm] all keys cooling, waiting {wait:.0f}s (attempt {attempt + 1})...")
            await asyncio.sleep(wait)

        server_err_wait = 0.0
        async with _semaphore():
            try:
                return await asyncio.to_thread(
                    call_gemini, system, user, model, temperature, 1, grounding, key
                )
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                is_retryable = (
                    "429" in err_str or "quota" in err_str
                    or "503" in err_str or "500" in err_str or "502" in err_str
                )
                if not is_retryable or attempt == max_retries - 1:
                    raise
                if "429" in err_str or "quota" in err_str:
                    _cool_key(key, _jittered(min(2 ** (attempt + 1), 30)))
                else:
                    server_err_wait = _jittered(min(2 ** (attempt + 1), 10))

        if server_err_wait > 0:
            print(f"  [llm] server error on attempt {attempt + 1}, retrying in {server_err_wait:.0f}s...")
            await asyncio.sleep(server_err_wait)

    raise RuntimeError(f"LLM failed after {max_retries} attempts: {last_err}")

def extract_json(text: str) -> dict | list:
    """Extract the first JSON object or array from a text response."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)

    for start_char, end_char in [('{', '}'), ('[', ']')]:
        idx = text.find(start_char)
        if idx == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(text[idx:], start=idx):
            if escape:
                escape = False
                continue
            if ch == '\\' and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[idx:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"No valid JSON found in response:\n{text[:400]}")

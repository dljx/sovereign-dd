"""Quick test of gemma-4-31b-it with and without Google Search grounding."""

import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

_raw_keys = os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", ""))
_keys = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if not _keys:
    raise RuntimeError("No GEMINI_API_KEYS found in .env")

client = genai.Client(api_key=_keys[0])
MODEL = "gemma-4-31b-it"

# ── Test 1: Plain call (no grounding) ─────────────────────────────────────────
print("=" * 60)
print("TEST 1: Plain call (no grounding)")
print("=" * 60)

r1 = client.models.generate_content(
    model=MODEL,
    contents="What is Vistra Corp's (VST) most recent quarterly earnings result and EPS?",
    config=types.GenerateContentConfig(
        temperature=0.3,
        max_output_tokens=512,
    ),
)
print(r1.text)

# ── Test 2: With Google Search grounding ──────────────────────────────────────
print()
print("=" * 60)
print("TEST 2: With Google Search grounding")
print("=" * 60)

r2 = client.models.generate_content(
    model=MODEL,
    contents="What is Vistra Corp's (VST) most recent quarterly earnings result and EPS? Include the date reported.",
    config=types.GenerateContentConfig(
        temperature=0.3,
        max_output_tokens=512,
        tools=[{"google_search": {}}],
    ),
)
print(r2.text)

# Print grounding sources
print()
print("--- Grounding Sources ---")
try:
    chunks = r2.candidates[0].grounding_metadata.grounding_chunks
    if chunks:
        for i, chunk in enumerate(chunks, 1):
            print(f"  [{i}] {chunk.web.title}")
            print(f"      {chunk.web.uri}")
    else:
        print("  (no grounding chunks returned)")
except Exception as e:
    print(f"  (could not read grounding metadata: {e})")

# ── Test 3: Financial analysis with grounding ──────────────────────────────────
print()
print("=" * 60)
print("TEST 3: Financial analysis prompt with grounding")
print("=" * 60)

r3 = client.models.generate_content(
    model=MODEL,
    contents=(
        "Analyse Vistra Corp (VST) as an investment right now. "
        "What are the latest analyst price targets, recent earnings surprises, "
        "and key risks? Give a 1-10 investment score with reasoning."
    ),
    config=types.GenerateContentConfig(
        temperature=0.4,
        max_output_tokens=1024,
        tools=[{"google_search": {}}],
    ),
)
print(r3.text)

print()
print("--- Grounding Sources ---")
try:
    chunks = r3.candidates[0].grounding_metadata.grounding_chunks
    if chunks:
        for i, chunk in enumerate(chunks, 1):
            print(f"  [{i}] {chunk.web.title}")
            print(f"      {chunk.web.uri}")
    else:
        print("  (no grounding chunks returned)")
except Exception as e:
    print(f"  (could not read grounding metadata: {e})")

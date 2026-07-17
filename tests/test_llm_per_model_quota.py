"""llm per-(key, model) quota tracking (2026-07-18).

Google's free-tier quotas are scoped per project AND per model (quotaId
GenerateRequestsPerDayPerProjectPerModel-FreeTier). On 2026-07-17 flash's new
20 RPD limit exhausted keys one by one, and llm.py — which tracked exhaustion
per KEY — treated them as dead for gemma too: the whole pool hit 0/6 mid-run,
MU's analysis was lost and a scout run produced 221 fabricated agent results.
These tests lock the model-scoped contracts: exhausting flash on every key
must leave gemma calls fully live, and cooldowns must not leak across models.
"""

import types

import pytest

import llm

FLASH = "gemini-3.5-flash"
GEMMA = "gemma-4-31b-it"

# The real 429 body captured live on 2026-07-17 (abridged to the fields the
# classifier reads). quotaDimensions carry the model — the proof the quota is
# per-model, and the fixture _is_daily_exhausted must keep classifying as daily.
_FLASH_DAILY_429 = (
    "429 RESOURCE_EXHAUSTED. You exceeded your current quota. "
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash "
    "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier "
    "quotaDimensions: location: global, model: gemini-3.5-flash"
)


@pytest.fixture
def two_keys(monkeypatch):
    """Two fake keys with clean per-model quota state; monkeypatch restores."""
    monkeypatch.setattr(llm, "_keys", ["kA", "kB"])
    monkeypatch.setattr(llm, "_key_cooldowns", {})
    monkeypatch.setattr(llm, "_key_daily_exhausted", set())
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_flash_exhaustion_leaves_gemma_live(two_keys):
    llm._exhaust_key("kA", FLASH)
    llm._exhaust_key("kB", FLASH)
    key, wait = llm._pick_key(FLASH)
    assert key is None                          # flash: pool truly dead
    key, wait = llm._pick_key(GEMMA)
    assert key == "kA" and wait == 0.0          # gemma: completely unaffected


def test_cooldown_is_model_scoped(two_keys):
    llm._cool_key("kA", GEMMA, 60)
    key, wait = llm._pick_key(FLASH)
    assert key == "kA" and wait == 0.0          # gemma cooldown must not delay flash
    key, wait = llm._pick_key(GEMMA)
    assert key == "kB" and wait == 0.0          # gemma itself rotates off the cooling key


def test_daily_classifier_on_the_real_payload():
    assert llm._is_daily_exhausted(_FLASH_DAILY_429.lower()) is True
    # A per-minute (RPM/TPM) violation must stay temporary — cool + rotate, not
    # a daily kill. No daily marker present -> False.
    tpm = ("429 quota exceeded for metric: generate_content_free_tier_"
           "input_token_count, limit: 16000, model: gemma-4-31b-it "
           "quotaId: GenerateContentInputTokensPerModelPerMinute-FreeTier")
    assert llm._is_daily_exhausted(tpm.lower()) is False


def _wire_fake_generate(monkeypatch, behavior):
    """Route call_gemini's client through `behavior(model) -> text | raise`."""
    def fake_generate(model=None, contents=None, config=None):
        text = behavior(model)
        return types.SimpleNamespace(
            text=text,
            candidates=[types.SimpleNamespace(finish_reason="STOP")],
        )

    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=fake_generate))
    monkeypatch.setattr(llm, "_client_for", lambda key: fake_client)
    monkeypatch.setattr(llm, "_resolve_model", lambda c, k, m: m)


def test_call_gemini_flash_death_does_not_poison_gemma(two_keys, monkeypatch):
    """The 2026-07-17 incident, replayed: every flash call hits the daily 429,
    the flash pool dies — and a gemma call on the very same keys still works."""
    def behavior(model):
        if model == FLASH:
            raise RuntimeError(_FLASH_DAILY_429)
        return "ok"

    _wire_fake_generate(monkeypatch, behavior)

    with pytest.raises(RuntimeError) as exc:
        llm.call_gemini("s", "u", model=FLASH, max_retries=4)
    assert FLASH in str(exc.value)              # error names the dead model
    assert ("kA", FLASH) in llm._key_daily_exhausted
    assert ("kB", FLASH) in llm._key_daily_exhausted

    assert llm.call_gemini("s", "u", model=GEMMA, max_retries=2,
                           thinking_level=None) == "ok"
    assert not any(m == GEMMA for _, m in llm._key_daily_exhausted)

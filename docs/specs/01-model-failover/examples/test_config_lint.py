"""ILLUSTRATIVE config-lint test (spec, not wired in) -> tests/test_failover_config.py.

CI-SAFE: pure YAML parsing, no network, no provider, no running gateway. Runs
unconditionally on every PR. Proves the static half of the acceptance criteria:
the chat alias has >=2 deployments, every deployment has a stable model_info.id,
a chat-standby alias exists, the ordered fallbacks chain is present, and the
router ejection knobs are set to the documented values.

PyYAML is not currently a dependency; the real test would either add it to the
dev group or parse with a stdlib-only loader. Shown with yaml for clarity.
"""
from collections import Counter
from pathlib import Path

import yaml

CONFIG = Path("gateway/litellm_config.yaml")


def _load():
    return yaml.safe_load(CONFIG.read_text())


def test_chat_alias_has_two_plus_same_model_deployments():
    cfg = _load()
    names = Counter(m["model_name"] for m in cfg["model_list"])
    assert names["chat"] >= 2, "chat alias must resolve to >=2 deployments (active/active)"


def test_every_deployment_has_stable_model_info_id():
    cfg = _load()
    for m in cfg["model_list"]:
        assert m.get("model_info", {}).get("id"), (
            f"{m['model_name']} needs a stable model_info.id so x-litellm-model-id "
            "is a readable name, not a per-process UUID"
        )


def test_chat_standby_alias_exists():
    cfg = _load()
    names = {m["model_name"] for m in cfg["model_list"]}
    assert "chat-standby" in names, "chat-standby must be its own entry so it is pinnable"


def test_ordered_fallback_chain_present_and_colocated():
    cfg = _load()
    # Co-located with the other router knobs (see README §2). Assert the chosen
    # block actually contains the chain; do not silently accept the other block.
    router = cfg.get("router_settings", {})
    assert {"chat": ["chat-standby"]} in router.get("fallbacks", []), (
        "fallbacks chat->chat-standby must live in router_settings"
    )


def test_router_ejection_values():
    cfg = _load()
    r = cfg["router_settings"]
    assert r["num_retries"] == 1
    assert r["allowed_fails"] == 1
    assert r["cooldown_time"] == 30


def test_drop_params_enabled():
    cfg = _load()
    assert cfg["litellm_settings"]["drop_params"] is True


def test_embeddings_not_failed_over_to_a_different_model():
    # Criterion 8 (negative guard): a future "helpful" cross-model embeddings
    # fallback would land vectors in a different space and silently corrupt
    # retrieval. Assert every `embeddings` deployment stays on the same embedding
    # model the pgvector index was built with (text-embedding-3-small). A
    # same-model standby from another provider (e.g. Azure text-embedding-3-small)
    # is allowed; a *different* model is not.
    cfg = _load()
    embed_models = [
        m["litellm_params"]["model"]
        for m in cfg["model_list"]
        if m["model_name"] == "embeddings"
    ]
    assert embed_models, "embeddings alias must exist"
    for model in embed_models:
        assert model.endswith("text-embedding-3-small"), (
            f"embeddings must stay the SAME model (text-embedding-3-small); got {model!r}. "
            "A different embedding model corrupts the pgvector index (see design.md)."
        )

"""ILLUSTRATIVE integration smoke test (spec, not wired in) -> tests/test_failover_smoke.py.

Proves the dynamic acceptance criteria: ordered fallback returns 200 from the
standby when the primary is killed, and the unhealthy deployment is EJECTED (not
hot-path-retried) so every follow-up in the cooldown window is served by the
standby.

Like the existing eval gate (tests/test_evals.py -> real model calls), this needs
a running gateway + a real OPENAI_API_KEY; it does NOT mock the gateway. It skips
cleanly when either is absent, and it never needs AWS (no-AWS path).

Assumes the gateway under test was started with the no-AWS config
(examples/litellm_config.no-aws.yaml): primary `chat` -> dead api_base,
`chat-standby` -> healthy OpenAI, model_info.id "chat-standby-openai".

The key technique: read the SERVED deployment from the raw response, because
app/gateway.chat() returns only the message string and discards resp.model /
headers. We use the OpenAI SDK's with_raw_response to read x-litellm-model-id.
"""
import os

import pytest
from openai import OpenAI

from app.config import settings

STANDBY_ID = "chat-standby-openai"   # matches model_info.id in the no-AWS config


def _gateway_up(client: OpenAI) -> bool:
    try:
        client.models.list()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def client():
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("no OPENAI_API_KEY; failover smoke test needs a real key")
    c = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)
    if not _gateway_up(c):
        pytest.skip("gateway not reachable; start the stack (make up) to run this")
    return c


def _call_served_by(client: OpenAI):
    """Return (status, served_deployment_id) reading the raw response/header."""
    raw = client.chat.completions.with_raw_response.create(
        model="chat",
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
    )
    served = raw.headers.get("x-litellm-model-id")
    resp = raw.parse()
    return raw.status_code, (served or resp.model)


def test_ordered_fallback_serves_standby(client):
    # Primary is dead (config-only kill: dead api_base). A chat request must still
    # 200, served by the standby.
    status, served = _call_served_by(client)
    assert status == 200
    assert served == STANDBY_ID, f"expected standby to serve, got {served!r}"


def test_unhealthy_deployment_is_ejected_not_retried(client):
    # With allowed_fails: 1 / cooldown_time: 30, the first failure ejects the dead
    # primary for the window. Every follow-up inside the window must be served by
    # the standby (ejection proven by WHO served, not by router internals), and
    # must return promptly (no dead-host retry/timeout latency).
    import time

    for _ in range(5):
        t0 = time.monotonic()
        status, served = _call_served_by(client)
        elapsed = time.monotonic() - t0
        assert status == 200
        assert served == STANDBY_ID
        assert elapsed < 5.0, "ejected primary must not add dead-host retry latency"


def test_drop_params_no_400(client):
    # `drop_params: true` means an extra/unsupported param is dropped, not 400'd.
    # NOTE: the no-AWS smoke path is single-provider (OpenAI only), so it cannot
    # exercise a genuinely *provider-unsupported* param — there is no second
    # provider to diverge from. This proves only "drop_params on + extra param does
    # not 400". True cross-provider param dropping is a production-only check
    # against the Anthropic+Bedrock config (litellm_config.prod.yaml), not a CI
    # gate. See ../testing.md criterion 6.
    raw = client.chat.completions.with_raw_response.create(
        model="chat",
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
        frequency_penalty=0.1,   # benign extra param; dropped if unsupported
    )
    assert raw.status_code == 200

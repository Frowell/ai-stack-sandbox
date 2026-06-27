"""ILLUSTRATIVE — spec for spec 13, not wired-in code.

Filename is `example_tests.py` (NOT test_*.py) on purpose so pytest's default
discovery does not collect it while it lives under docs/specs/. When implementing,
port these into:
  - tests/test_structured_outputs.py  (the new offline unit tests)
  - tests/test_evals.py               (reconcile the existing live gate test)

Every test here runs with NO network / NO API key by stubbing the gateway seam
(`app.gateway._client.chat.completions.create`) via monkeypatch. That is the
proof the SCHEMA, not the provider, is authoritative.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _resp(content: str):
    """Mimic the shape app code reads: resp.choices[0].message.content."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# --------------------------------------------------------------------------- #
# chat_structured — the gateway seam
# --------------------------------------------------------------------------- #

def test_valid_json_returns_typed_instance(monkeypatch):
    """AC: valid output -> validated pydantic instance."""
    from app import gateway
    from app.evals import JudgeVerdict

    monkeypatch.setattr(
        gateway._client.chat.completions, "create",
        lambda **kw: _resp('{"score": 0.8}'),
    )
    out = gateway.chat_structured([{"role": "user", "content": "x"}], JudgeVerdict)
    assert isinstance(out, JudgeVerdict)
    assert out.score == 0.8


def test_dropped_param_freetext_raises(monkeypatch):
    """AC: drop_params returns free text with NO transport error; client-side
    validation must still reject it. The retry means create() is called TWICE,
    so the stub must return non-conforming text on BOTH calls.
    """
    from app import gateway
    from app.evals import JudgeVerdict

    calls = {"n": 0}

    def fake_create(**kw):
        calls["n"] += 1
        return _resp("I'd say about 0.8 out of 1.0")  # prose, not JSON

    monkeypatch.setattr(gateway._client.chat.completions, "create", fake_create)
    with pytest.raises(gateway.StructuredOutputError):
        gateway.chat_structured([{"role": "user", "content": "x"}], JudgeVerdict)
    assert calls["n"] == 2  # one bounded retry happened (design.md §5)


def test_invalid_then_valid_succeeds_after_retry(monkeypatch):
    """AC: the corrective retry can recover; create() invoked twice."""
    from app import gateway
    from app.evals import JudgeVerdict

    seq = iter([_resp("not json"), _resp('{"score": 0.6}')])
    monkeypatch.setattr(
        gateway._client.chat.completions, "create", lambda **kw: next(seq)
    )
    out = gateway.chat_structured([{"role": "user", "content": "x"}], JudgeVerdict)
    assert out.score == 0.6


def test_retry_preserves_role_alternation(monkeypatch):
    """AC #3: the corrective retry appends the rejected reply as an `assistant`
    turn THEN a `user` corrective turn — never a 2nd consecutive `user` turn and
    never a 2nd `system` turn — so the request is valid under both the OpenAI and
    the advertised Anthropic mapping of the `chat` alias. Inspect the messages on
    the SECOND `create` call (design.md §5).
    """
    from app import gateway
    from app.evals import JudgeVerdict

    seen: list[list[dict]] = []
    seq = iter([_resp("not json"), _resp('{"score": 0.6}')])

    def fake_create(**kw):
        seen.append(kw["messages"])
        return next(seq)

    monkeypatch.setattr(gateway._client.chat.completions, "create", fake_create)
    out = gateway.chat_structured(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        JudgeVerdict,
    )
    assert out.score == 0.6
    second = seen[1]                               # messages on the retry call
    roles = [m["role"] for m in second]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles  # no consecutive same-role
    assert roles.count("system") == 1                            # no 2nd system turn
    assert roles[-1] == "user"                                   # last is the corrective
    assert "schema" in second[-1]["content"].lower()


def test_strict_schema_shape():
    """AC: the schema sent under strict:true carries additionalProperties:false
    and lists all fields in required, and drops title — so the provider does not
    400 on a bare pydantic schema.
    """
    from app.evals import JudgeVerdict
    from app.gateway import _strict_schema

    s = _strict_schema(JudgeVerdict)
    assert s["additionalProperties"] is False
    assert s["required"] == ["score"]
    assert "title" not in s


def test_strict_schema_rejects_nested():
    """AC (v1 scope): a nested schema ($defs) raises loudly, not a silent 400."""
    from pydantic import BaseModel

    from app.gateway import _strict_schema

    class Inner(BaseModel):
        v: int

    class Outer(BaseModel):
        inner: Inner

    with pytest.raises(ValueError):
        _strict_schema(Outer)


# --------------------------------------------------------------------------- #
# evals.run — the eval-gate refactor
# --------------------------------------------------------------------------- #

def test_judge_error_excluded_from_mean(monkeypatch, tmp_path):
    """AC: a judge failure is recorded as a per-case error, score is None, the
    case is EXCLUDED from mean_score, and gate_status is 'eval_error' (not a 0.0
    quality fail). This is the literal original-bug regression test.
    """
    from app import evals

    golden = tmp_path / "g.jsonl"
    golden.write_text(
        '{"question": "q1", "keywords": ["a"], "reference": "r1"}\n'
        '{"question": "q2", "keywords": ["a"], "reference": "r2"}\n'
    )
    monkeypatch.setattr(evals, "ask", lambda q: "a")  # keyword_score -> 1.0

    def judge(question, answer, reference):
        if question == "q2":
            raise evals.StructuredOutputError("JudgeVerdict", "prose", cause=None)  # type: ignore[arg-type]
        return 1.0

    monkeypatch.setattr(evals, "judge_score", judge)

    report = evals.run(str(golden))
    cases = {c["question"]: c for c in report["cases"]}
    assert cases["q2"]["score"] is None and cases["q2"]["error"] is not None
    assert cases["q1"]["score"] == 1.0           # 0.5*1 + 0.5*1
    assert report["mean_score"] == 1.0           # q2 NOT averaged in as 0.0
    assert report["gate_status"] == "eval_error"
    assert report["passed"] is False             # fail loud, but labeled


def test_low_valid_score_is_quality_fail(monkeypatch, tmp_path):
    """AC #9 (the OTHER branch): a low BUT VALID score with NO errored case must
    label `gate_status == "quality_fail"`, distinct from the `eval_error` infra
    path. Without this the central eval_error/quality_fail distinction is only
    half-proven.
    """
    from app import evals

    golden = tmp_path / "g.jsonl"
    golden.write_text('{"question": "q1", "keywords": ["zzz"], "reference": "r1"}\n')
    monkeypatch.setattr(evals, "ask", lambda q: "a")            # keyword miss -> 0.0
    monkeypatch.setattr(evals, "judge_score", lambda *a: 0.0)   # valid, low score

    report = evals.run(str(golden))
    assert report["cases"][0]["error"] is None                 # NOT an infra error
    assert report["mean_score"] == 0.0                         # 0.5*0 + 0.5*0
    assert report["gate_status"] == "quality_fail"             # the other branch
    assert report["passed"] is False


def test_format_report_is_none_safe():
    """AC #7: the report-print path tolerates None — an errored case and an
    all-errored (None) mean render labels, never crash on `:.2f` or a
    `None >= THRESHOLD` comparison. Calls the importable `format_report` helper
    directly (the formatting must NOT be trapped inside `__main__`).
    """
    from app.evals import format_report

    report = {
        "mean_score": None,                # all-errored -> None mean
        "passed": False,
        "gate_status": "eval_error",
        "cases": [
            {"question": "q1", "score": None, "error": "StructuredOutputError: prose", "passed": False},
            {"question": "q2", "score": 0.90, "error": None, "passed": True},
        ],
    }
    text = "\n".join(format_report(report))
    assert "[ERR ]" in text          # errored case labeled, not formatted as a number
    assert "n/a" in text             # None mean rendered, not a :.2f crash
    assert "0.90" in text            # valid case still formatted
    assert "status=eval_error" in text


def test_all_cases_error_mean_is_none(monkeypatch, tmp_path):
    """AC: if every case errors, mean_score is None — no divide-by-zero, no 0.0."""
    from app import evals

    golden = tmp_path / "g.jsonl"
    golden.write_text('{"question": "q1", "reference": "r1"}\n')
    monkeypatch.setattr(evals, "ask", lambda q: "a")

    def boom(*a, **k):
        raise evals.StructuredOutputError("JudgeVerdict", "prose", cause=None)  # type: ignore[arg-type]

    monkeypatch.setattr(evals, "judge_score", boom)

    report = evals.run(str(golden))
    assert report["mean_score"] is None
    assert report["gate_status"] == "eval_error"
    assert report["passed"] is False


# --------------------------------------------------------------------------- #
# Reconcile the EXISTING live gate test (acceptance criterion: no silent network)
# Option (a): skip when no gateway/key is configured. State the choice explicitly.
# --------------------------------------------------------------------------- #

def test_quality_gate():
    """The original live end-to-end gate. Make its network dependency HONEST:
    skip clearly when no gateway/key is configured instead of erroring offline.
    """
    import os

    from app.evals import run

    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("live eval gate needs a reachable gateway + OPENAI_API_KEY")
    report = run()
    assert report["passed"], f"quality gate failed: status={report['gate_status']} mean={report['mean_score']}"

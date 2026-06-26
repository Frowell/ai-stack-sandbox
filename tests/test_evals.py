"""Wiring the eval suite into pytest makes 'evals as a merge gate' literal:
`uv run pytest` fails the build when answer quality regresses below threshold.
"""
from app.evals import run


def test_quality_gate():
    report = run()
    assert report["passed"], f"quality gate failed: mean={report['mean_score']}"

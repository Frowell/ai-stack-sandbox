"""Wiring the eval suite into pytest makes 'evals as a merge gate' literal:
`uv run pytest` fails the build when answer quality regresses below threshold.
"""
from app.evals import run


def test_quality_gate():
    report = run()
    failed = [c["name"] for c in report["checks"] if not c["passed"]]
    assert report["passed"], (
        f"quality gate failed: overall={report['overall']} weighted={report['weighted']} "
        f"failed_checks={failed} alerts={report['alerts']}"
    )

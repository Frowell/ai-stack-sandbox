"""ILLUSTRATIVE — spec for tests/test_render_context.py (NOT wired in).

This is the PRIMARY feature proof: deterministic, no gateway, no DB. It pins all
four sub-criteria of acceptance criterion 2 on the pure render_context() helper.
Matches the repo's pytest idiom (cf. tests/test_evals.py): plain functions,
`from app... import ...`, asserts.
"""
from app.agent import render_context
from app.retrieval import RetrievedChunk


# (a) section breadcrumb is surfaced as a parenthetical.
def test_section_is_rendered():
    out = render_context([RetrievedChunk(3, "body", {"section": "A > B"})])
    assert out == "[3] (A > B) body"
    assert "(A > B)" in out


# (b) no displayable key => BYTE-IDENTICAL to today's format, including the
#     "\n\n" join across a LIST (the whole-context level, not just one line).
def test_no_key_is_byte_identical_to_today():
    chunks = [
        RetrievedChunk(1, "alpha", {"doc": "maturity"}),   # only non-displayable keys
        RetrievedChunk(2, "beta", {}),                     # empty meta
    ]
    today = "\n\n".join(f"[{c.id}] {c.content}" for c in chunks)  # the old expression
    assert render_context(chunks) == today
    assert "(" not in render_context(chunks)               # no stray parenthetical


# (c) page present => "p.N"; page absent => no "(p." and no raise.
def test_page_present_and_absent():
    with_page = render_context([RetrievedChunk(4, "x", {"section": "S", "page": 7})])
    assert with_page == "[4] (S, p.7) x"                   # section first, then page
    only_page = render_context([RetrievedChunk(5, "y", {"page": 9})])
    assert only_page == "[5] (p.9) y"
    no_page = render_context([RetrievedChunk(6, "z", {"section": "S"})])
    assert "(p." not in no_page                            # no empty page marker


# (d) meta is None (a NULL DB row) renders like the no-key case and does NOT raise.
def test_none_meta_does_not_raise():
    out = render_context([RetrievedChunk(7, "w", None)])   # NULL row -> psycopg None
    assert out == "[7] w"                                  # identical to no-key case

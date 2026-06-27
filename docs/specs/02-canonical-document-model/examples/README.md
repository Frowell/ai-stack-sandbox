# Examples — illustrative only

These files are a **spec, not shipped code.** They show the intended shapes,
signatures, and file paths for the [canonical-document-model](../README.md)
feature so a reviewer can judge the design and an implementer has a concrete
target. They are **not** wired into the app and are **not** collected by the test
suite:

- They live under `docs/specs/…`, outside the `app/` package and the `tests/`
  dir that `uv run pytest` runs against.
- The example test file is named `example_tests.py` (not `test_*.py` /
  `*_test.py`), so pytest's default discovery does **not** pick it up. Do not
  rename it into a collected pattern while it lives here.

> ⚠️ **Doubly illustrative.** This feature *refactors PR #2's* `app/layout.py`
> and `app/chunking.py`, which **do not exist in the tree yet** (verified: no
> `app/layout.py`, no `app/chunking.py`, no `meta` column in `db/init.sql`, and
> `app/ingest.py` ingests a flat `{source,content}` corpus into
> `(source, content, embedding)`). So these examples sketch *both* PR #2's assumed
> v0 shape and the v1 target. Re-baseline against PR #2's actual merge before
> porting — see the README *Open questions* / *Risks*.

| File | Mirrors / would change | Shows |
| --- | --- | --- |
| [`example_layout.py`](example_layout.py) | `app/layout.py` (PR #2) | the v1 types (`Document`/`Block`/`Locator`/`Relation`, closed `BlockType`/`RelationType`, `SCHEMA_VERSION`), construction-time enum enforcement, the v0→v1 `kind` mapping, and the reject-unknown-major reader guard |
| [`example_chunking.py`](example_chunking.py) | `app/chunking.py` (PR #2) | `chunk_layout` attaching footnotes by **walking `footnote_ref` relations** (not the `[^id]` token), one block-id namespace in `meta`, and the trailing uncited-footnote chunk |
| [`example_ingest.py`](example_ingest.py) | `app/ingest.py` | both ingest paths stamping `meta` (layout chunks vs back-compat `{source,content}` raw rows), the single `SCHEMA_VERSION`, and the `meta`-column precondition |
| [`canonical_document.sample.json`](canonical_document.sample.json) | a serialized `Document` | the corrected worked example (one block-id namespace: `meta.footnotes` uses **block ids**, fixing `CANONICAL_MODEL.md`'s `to_id:"b3"` vs `footnotes:["otel"]` inconsistency) |
| [`example_tests.py`](example_tests.py) | new `tests/test_canonical_model.py` (+ keeps PR #2's footnote tests) | offline, stubbed tests proving each acceptance criterion |

When implementing, port the relevant pieces into the real files under `app/`,
`tests/`, and (if PR #2 has not already) `db/` — **do not import from this
directory.**

---
title: Multi-modal ingestion
slug: multi-modal-ingestion
area: ingestion
tier: Horizon
size: L
status: Backlog
depends_on: [canonical-document-model, real-layout-backends, retrieval-uses-chunk-metadata]
issue:        # set to the GitHub issue number when created
---

# Multi-modal ingestion

> **Area** `ingestion` · **Tier** `Horizon` · **Size** `L` · **Status** `Backlog` · **Depends on:** [canonical-document-model](../02-canonical-document-model/README.md), [real-layout-backends](../03-real-layout-backends/README.md), [retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)

## Summary

Ingestion, retrieval, and generation are text-only today: `app/ingest.py` embeds
plain `content` strings into a single `documents.embedding VECTOR(1536)` column,
and `generate_node` flattens retrieved chunks into a text-only prompt. This spec
extends the pipeline so an **image-bearing** document (and, secondarily, audio)
ingests into canonical `figure`/image blocks, is retrievable, and can be reasoned
over by a vision-capable model — *without* the agent, gateway, or eval-gate
contracts changing shape. The seam decision is deliberately conservative:
**describe-then-embed** (caption/transcribe non-text into the existing text
space) is the default path, with a separate native image-vector space called out
as an explicit, deferred option. This keeps RRF, the FTS column, and the single
embedding space intact while still demonstrating the multi-modal seam end to end.

## Problem / Motivation

Ingestion and retrieval are text-only; real corpora include images (charts,
diagrams, scanned figures) and audio (recordings, voice notes). The canonical
model v1 reserves a `figure` `BlockType` but **explicitly defers image/audio
payloads** ([`CANONICAL_MODEL.md` → Deferred](../../CANONICAL_MODEL.md)); the
layout backends (PR #3) emit `figure` blocks as references with no payload. Until
this spec lands there is no path from a non-text source to a retrievable,
reasoned-over chunk.

## Goals

- A canonical-model extension (additive, `schema_version` bump) that lets a
  `figure`/`audio` block carry a **payload reference** (storage key + media
  type + optional extracted text), not just an inline reference.
- An ingestion path that, for each non-text block, produces a **searchable text
  surrogate** (image caption via the vision `chat` alias; audio transcript via a
  gateway transcription route) and embeds that surrogate into the existing text
  space, while persisting the binary out-of-band.
- A retrieval path that can return a non-text chunk (matched via its surrogate)
  and a `generate_node` that passes the underlying image to a vision-capable
  model as an OpenAI-style image content part — so the agent reasons over the
  pixels, not just the caption.
- A multi-modal golden fixture and an **eval-gate** assertion (not just a manual
  demo) proving the non-text chunk is retrieved and reasoned over.

## Non-goals

- Audio transcription quality tuning (use a provider as-is).
- OCR for scanned PDFs (covered/excluded by [real-layout-backends](../03-real-layout-backends/README.md)).
- A native cross-modal (CLIP-style) image vector space as the *default* — called
  out as a deferred option below, not built in the first slice.
- Video, and real-time/streaming media.
- Migrating the existing text corpus; this is purely additive.

## Proposed design

The schema lives in [`CANONICAL_MODEL.md`](../../CANONICAL_MODEL.md); this section
is the implementation seam, the storage decision, and the rollout path. It cannot
start until its two dependencies ship (see **Sequencing** and Open questions).

**Seam.** Multi-modal lives behind the same extractor → chunker → ingest →
retrieve → generate seam as text, plus one new sub-seam (`surrogate()`), so no
new top-level component is introduced.

- **Schema (canonical-model v1.x, additive).** Add an optional `payload` to
  `Block`: `{ media_type, storage_key, text? }`. `text` is the surrogate
  (caption/transcript). Two additive changes, both major-version-preserving per
  the `CANONICAL_MODEL.md` versioning rule: (a) the new optional `payload` field,
  and (b) a new `audio` value appended to the closed `BlockType` enum (`figure`
  already exists). Note (b) interacts with spec 02's "construction rejects
  unknown `type`" validation: appending `audio` is additive for *forward*
  consumers but an *older* validator (built before this bump) will reject it — so
  this enum value must land in `CANONICAL_MODEL.md` and spec 02's enum together,
  not as a side addition here. Consumers that ignore `payload` still parse. This
  unblocks the item the canonical model deferred.
- **Storage (binary).** The `documents` table stays text+vector only. Binary
  payloads are written out-of-band keyed by `storage_key`: filesystem under a
  configured `MEDIA_DIR` for the sandbox (object store in a real deployment).
  No bytea in Postgres, no second embedding column in the default path.
  - **`storage_key` is content-addressed** (e.g. `sha256(bytes)` +
    media-type-derived extension), never derived from the source filename or any
    untrusted document field. This makes ingest idempotent (re-ingesting the same
    bytes overwrites the same key) and removes a path-injection surface (see
    Risks → *Untrusted `storage_key` / path traversal*).
  - **Re-ingest is destructive today** (`app/ingest.py` does
    `TRUNCATE documents RESTART IDENTITY`), which would orphan every blob in
    `MEDIA_DIR`. The ingest step must either (i) clear `MEDIA_DIR` in the same
    operation as the TRUNCATE, or (ii) garbage-collect blobs whose `storage_key`
    is no longer referenced by any row, so re-ingest does not leak binaries.
    Pick (i) for the sandbox slice; note (ii) as the real-deployment shape.
- **Surrogate generation (`app/ingest.py` new `surrogate()` step).** For each
  non-text block: images → a caption via the vision `chat` alias (gpt-4o-mini is
  already vision-capable; the Anthropic swap is also vision-capable, and LiteLLM
  normalizes the image content-part shape across providers); audio → a transcript
  via a gateway transcription route. The surrogate text is what gets embedded and
  what populates the FTS column — so **RRF, the single 1536-dim space, and the
  `fts` column are unchanged**. The chunk's `content` is the surrogate;
  `meta.payload` carries `storage_key`/`media_type`.
  - **Vision capability is not introspectable from the alias.** The app only ever
    names the opaque `chat` alias (`config.chat_model = "chat"`); it cannot tell
    whether the model the gateway routes that alias to supports image input. So
    vision capability is declared by config, **not** detected at runtime: a new
    `CHAT_MODEL_SUPPORTS_VISION` bool (default `false`) gates every image→model
    call. Captioning at ingest **requires vision** — vision is needed at *ingest*
    (to caption), not only at *generate*. When `CHAT_MODEL_SUPPORTS_VISION=false`
    (or `MULTIMODAL_ENABLED=false`), `surrogate()` does **not** call the model with
    an image; it falls back to a deterministic non-empty placeholder surrogate
    (media type + source/block locator, e.g. `"[figure: <source> p.<n>]"`) so the
    chunk is still embeddable and retrievable, and logs that captioning was
    skipped. (Optional hardening: the app may consult the gateway's
    `/model/info` to verify the flag matches reality, but the flag is the source
    of truth — the app never branches on a provider name.)
  - **Empty/failed surrogate is never embedded as-is.** If the captioner/transcriber
    returns empty or whitespace (or errors after retry), `surrogate()` substitutes
    the same deterministic placeholder above rather than embedding an empty string
    (an empty `content` produces a degenerate embedding and an empty `fts`, making
    the chunk silently unretrievable and risking divide-by-zero in scorers). The
    binary is still persisted under `MEDIA_DIR`; the chunk is still
    generate-time-resolvable via `meta.payload`.
- **Provider calls go through the gateway.** Captioning uses the existing `chat`
  alias. Audio transcription is added as a gateway route
  (`audio/transcriptions`, LiteLLM-supported) so the "even embeddings go through
  the gateway" invariant holds; the app never names a transcription provider.
- **Retrieval (`app/retrieval.py`) — unchanged hot path, but built *on top of*
  spec 05, not in parallel with it.** A non-text chunk is retrieved exactly like
  text because its surrogate is in both the dense and sparse spaces. Surfacing
  `meta` from `retrieve()` (the selectlist + return-type change from
  `list[tuple[int, str]]` to a payload-carrying shape) is **owned by
  [retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)**;
  this spec must *consume* that, not re-implement a competing selectlist change.
  Spec 05 is therefore added as a hard dependency. Once `meta` is threaded
  through, this spec reads `meta.payload` to know which chunks have a binary to
  load. (The `meta JSONB` column itself does **not** exist in `db/init.sql`
  today; it lands as part of the PR #2 / spec 02 ingestion work that specs 05 and
  18 both build on. Spec 05's README phrases this as "PR #2 adds `meta JSONB`" —
  same column, same prerequisite; this spec does not add it.)
- **Generation (`app/agent.py`).** `generate_node` builds OpenAI-style content
  parts: text chunks as today; a chunk with an image payload adds an
  `{type: "image_url", image_url: {...}}` part (base64 from `MEDIA_DIR`). Guard
  total image bytes/count per request (config cap) to bound token cost and stay
  under provider limits. **Truncation is deterministic:** images are admitted in
  retrieval-rank order and the lowest-ranked ones are dropped when a cap is hit,
  so the same query always yields the same prompt; dropped images are logged with
  their chunk id. **Vision is config-declared, not detected:** the image content
  part is only constructed when `CHAT_MODEL_SUPPORTS_VISION=true` (see Surrogate
  generation — the app cannot introspect the opaque alias). When the flag is
  `false`, `generate_node` falls back to the surrogate text (graceful
  degradation, logged) — the same flag governs ingest captioning, so the two
  paths never disagree about whether the model can see pixels.
  **Context-shape compatibility:**
  when `MULTIMODAL_ENABLED=false`, `generate_node` and the `State.context` shape
  behave exactly as today (text path untouched); the payload-carrying part is
  only constructed for chunks that actually have a `meta.payload` *and* the flag
  on.
- **Config.** `MEDIA_DIR`, `MAX_IMAGES_PER_REQUEST`, `MAX_IMAGE_BYTES`,
  `MULTIMODAL_ENABLED` (default off), and `CHAT_MODEL_SUPPORTS_VISION` (default
  off — declares whether the model behind the `chat` alias accepts image input,
  since the app cannot introspect the alias). Feature-flagged so the text-only
  path is untouched when disabled. The multi-modal eval gate requires **both**
  `MULTIMODAL_ENABLED=true` and `CHAT_MODEL_SUPPORTS_VISION=true`; with vision off
  the pipeline still ingests/retrieves (placeholder surrogate) but the
  pixels-not-caption assertion is skipped, not failed.

**Sequencing (hard prerequisite).** Requires (1) [canonical-document-model](../02-canonical-document-model/README.md)
shipped (the `Block`/`payload` shape and `schema_version` discipline) and
(2) [real-layout-backends](../03-real-layout-backends/README.md) shipped (a
backend that actually emits `figure` blocks from a real document), and
(3) [retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)
shipped (the `meta`-carrying `retrieve()` surface this spec reads `payload` from).
All three are currently `Todo`/`Backlog` and unimplemented. This spec **cannot
enter implementation** until they land; see Open questions.

## Acceptance criteria

- [ ] Canonical model gains an optional `Block.payload` (`media_type`,
      `storage_key`, optional surrogate `text`); the change is additive (major
      `schema_version` unchanged) and documented in `CANONICAL_MODEL.md`.
- [ ] A golden image-bearing document ingests into canonical `figure`/image
      blocks; the binary is persisted under `MEDIA_DIR` and referenced by a
      **content-addressed `storage_key`**; a text surrogate (caption) is embedded
      into the existing 1536-dim space and FTS column — no schema/DB migration of
      `documents`.
- [ ] Re-running ingest is **idempotent for media**: it does not leak orphaned
      blobs in `MEDIA_DIR` (clear-on-TRUNCATE or GC of unreferenced keys), and
      re-ingesting identical bytes reuses the same `storage_key`.
- [ ] Reading a payload at generate time **resolves `storage_key` strictly under
      `MEDIA_DIR`** (canonicalized path must stay within `MEDIA_DIR`); a
      `storage_key` containing traversal (`..`, absolute paths) is rejected, not
      read. A test asserts a crafted traversing key cannot exfiltrate a file.
- [ ] Retrieval returns the non-text chunk for a query that matches its
      surrogate, via the unchanged hybrid (dense + sparse + RRF) path, using the
      `meta`-carrying return shape from **spec 05** (not a parallel selectlist).
- [ ] `generate_node` passes the underlying image to the vision `chat` alias as
      an image content part **only when `CHAT_MODEL_SUPPORTS_VISION=true`**; when
      that flag is `false` it degrades to surrogate text without error and without
      attempting an image call (the app never introspects the opaque alias — vision
      capability is config-declared). A test asserts the flag-off path constructs
      no image content part.
- [ ] Captioning at **ingest** respects the same `CHAT_MODEL_SUPPORTS_VISION`
      flag: with vision off, `surrogate()` writes a deterministic non-empty
      placeholder surrogate (media type + locator) instead of calling the model,
      so the chunk is still embeddable/retrievable. A captioner returning empty or
      erroring (after retry) yields the same placeholder — **no empty `content` is
      ever embedded**. A test asserts a forced-empty caption still produces a
      retrievable chunk.
- [ ] Per-request image count/bytes are capped by config; exceeding the cap
      truncates **deterministically by retrieval rank** (drop lowest-ranked,
      logged), not a hard crash.
- [ ] The multi-modal golden item proves the answer comes from **pixels, not the
      caption**: (a) the caption prompt is descriptive, **not** value-extractive
      (it must not transcribe the chart's load-bearing value into the surrogate
      text), and (b) the eval includes a **caption-only baseline that FAILS** the
      same item when the image content part is withheld — so passing requires the
      image, turning the demo into a real assertion. To keep the gate stable the
      multi-modal item uses `temperature=0` and keyword-based scoring.
      **Harness reality:** `app/evals.py run()` today only calls `ask(question)`,
      and `ask()`/`generate_node()`/`chat()` plumb no flag or `temperature`. So
      this assertion is **not** expressible against the current harness — the
      slice must either (i) thread `temperature` (and an image-withheld toggle)
      through `ask`→`generate_node`→`chat`, or (ii) implement the baseline as a
      standalone pytest that invokes the agent twice (image-on vs image-withheld)
      outside `run()`. Prefer (ii) for the first slice to avoid changing the
      shared `ask()` signature the text path depends on.
- [ ] The multi-modal eval item runs behind a **dedicated flag-on gate**
      (`MULTIMODAL_ENABLED=true` with the required key), and is **not** added to
      the default text-only golden suite (`evals/golden.jsonl`) — so the
      default-off invariant below and the multi-modal assertion are not mutually
      contradictory. **Concretely:** the repo has no `.github/workflows` today;
      the gate is `make test` / `pytest` (`tests/test_evals.py`) and `make eval`
      (`app/evals.py run(path)`). This slice therefore ships (a) a separate
      golden file (e.g. `evals/golden_multimodal.jsonl`) run via a new
      `make eval-mm` / a flag-gated pytest, and (b) the actual CI workflow (or a
      documented invocation) that runs it with the flag on — creating that CI
      surface is in-scope, not assumed. The default `make test` is untouched.
- [ ] Audio path: transcription routes through the gateway via a new
      `transcribe()` primitive in `app/gateway.py` (none exists today); an audio
      block ingests with its transcript as the surrogate. (May ship as a
      follow-up slice — see Risks.)
- [ ] `MULTIMODAL_ENABLED=false` (default) leaves the text-only pipeline
      byte-for-byte unchanged: same `retrieve()` return shape, same prompt, same
      default-suite scores; all existing tests pass.

## Dependencies

- [canonical-document-model](../02-canonical-document-model/README.md) — needs
  the `Block`/`payload` extension and `schema_version` discipline. (Currently
  `Todo`; multi-modal payloads are *explicitly deferred* there to here.)
- [real-layout-backends](../03-real-layout-backends/README.md) — needs a backend
  that emits real `figure` blocks. (Currently `Todo`.)
- [retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)
  — **hard dependency for the retrieval surface.** It owns threading `meta`
  through `retrieve()` (selectlist + return-type change). This spec consumes that
  to read `meta.payload`; it must not ship a competing selectlist change. (Also
  `Todo`, depends on the same PR #2 / `meta JSONB` column.)
- Indirectly relevant: [data-residency](../16-data-residency/README.md) (sending
  media to a provider) and [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md)
  (vision/transcription cost).

## Open questions

- **Surrogate vs. native vectors.** Default is describe-then-embed into the
  shared text space. A native image-vector space (separate table + dim, queried
  in parallel and RRF-merged) is more faithful but doubles the retrieval
  substrate and breaks the "single embedding space" simplicity. Decision:
  ship surrogate first; revisit native vectors only if recall is demonstrably
  poor. (Leaning: defer.)
- **Audio in the first slice or a follow-up?** Transcription adds a new gateway
  route and a heavier external dependency. Proposal: land image end-to-end
  first; audio as a fast follow behind the same flag.
- **Storage backend in the sandbox.** Filesystem `MEDIA_DIR` is simplest and
  matches the "deliberately small slice" ethos, but isn't shared across
  containers without a volume mount. Confirm the compose volume story.
- **Can implementation start?** No — blocked on specs 02, 03, and (for the
  retrieval surface) 05, all `Todo`. This stays `Backlog` until at least 02
  ships. Tracked as the accepted risk below.
- **No CI surface exists yet.** The project has no `.github/workflows`; the
  "eval gate as a merge gate" is `make test`/`pytest` + `make eval` run by
  whatever wraps them. The spec's "flag-on CI job" therefore means *creating* a
  flag-gated runner (separate golden file + `make eval-mm`/pytest) and, if a
  GitHub Actions workflow is wanted, authoring it here. Accepted as in-scope
  work, not a found dependency. (Leaning: ship the `make`/pytest runner; treat
  the GHA workflow as optional polish.)
- **Caption leakage vs. the eval assertion.** A vision captioner is good enough
  to transcribe a chart's value straight into the surrogate `text`, which would
  let a caption-only answer pass and quietly defeat the "reasoned over pixels"
  claim. Decision: constrain the caption prompt to be descriptive-not-extractive
  **and** keep the caption-only baseline assertion (above) as the real guard,
  rather than trusting prompt discipline alone.

## Risks & mitigations

- **Blocked by unbuilt prerequisites (accepted risk).** 02 and 03 are not
  implemented and the canonical model defers multi-modal payloads. *Mitigation:*
  keep `Backlog`; do not open a feature directory or branch until 02 ships; the
  `payload` shape here is the concrete ask handed to spec 02's owner.
- **Cost / latency blow-up.** Vision (esp. high-res) and transcription are far
  more expensive and slower than text. *Mitigation:* per-request image caps,
  caption-at-ingest (pay once, not per query), feature flag, and cross-link to
  [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md).
- **Untrusted binary input.** Malformed/oversized media, decompression bombs,
  and remote `image_url` fetched provider-side (SSRF). *Mitigation:* validate
  media type and size at ingest; store and send only local base64 (no
  provider-fetched remote URLs); enforce `MAX_IMAGE_BYTES`.
- **Untrusted `storage_key` / path traversal (local file disclosure).** At
  generate time the app reads bytes from a path built from `storage_key` (which
  rides in document-derived `meta jsonb`) and base64-sends them to a provider. A
  crafted key (`../../etc/...`, absolute path) would exfiltrate arbitrary
  host files to the provider — a trust-boundary hole the SSRF mitigation above
  does **not** cover. *Mitigation:* keys are content-addressed (app-generated,
  not from the source), and every read canonicalizes the resolved path and
  asserts it is contained within `MEDIA_DIR`, rejecting anything outside.
- **Eval flakiness from live vision calls.** Gating CI on a non-deterministic
  vision response can spuriously block merges. *Mitigation:* `temperature=0`
  plus keyword scoring on the multi-modal item; run it in a dedicated flag-on CI
  job so a provider/key outage degrades that job, not the default text gate.
- **Data residency / third-party exposure.** Captioning/transcription send
  document media to a provider. *Mitigation:* same gateway boundary as text;
  defer region-pinning to [data-residency](../16-data-residency/README.md);
  note it explicitly so it isn't a surprise.
- **Provider portability.** Image content-part and transcription shapes differ
  across providers. *Mitigation:* rely on LiteLLM normalization + `drop_params`;
  add a provider-swap test (OpenAI vision ↔ Anthropic vision) to the seam suite.
  Because a live two-provider test needs a second API key CI may not have, this
  test is **opt-in / mocked at the LiteLLM boundary by default** (asserting the
  normalized content-part shape), and only runs live when both keys are present —
  it does not become a hard CI gate.
- **Eval can't see pixels (accepted risk, narrowed).** The keyword/LLM-judge
  scorer is text-only. *Mitigation:* the multi-modal golden item is crafted so
  the correct answer is *only* derivable from the image (e.g. a value shown only
  in a chart), so a text-only or caption-only answer fails the gate — turning the
  demo into a real assertion without a new scorer.

## Test & rollout plan

- **Unit:** `surrogate()` produces a caption/transcript and a `payload`;
  `generate_node` emits image content parts under the flag and falls back to text
  for a non-vision model; per-request caps truncate rather than crash.
- **Integration:** one image golden fixture round-trips ingest → retrieve →
  generate; the answer requires the image content. One provider-swap test
  (vision OpenAI ↔ Anthropic). Audio fixture if audio is in-slice.
- **Eval gate:** the multi-modal item runs as a **dedicated flag-on gate**
  (`MULTIMODAL_ENABLED=true`) over a separate golden file
  (`evals/golden_multimodal.jsonl`), kept out of the default text-only suite, so
  a regression fails (non-zero exit) without entangling the default-off
  invariant. Because the repo has **no `.github/workflows` today** (the gate is
  `make test`/`pytest` + `make eval`), this slice adds the runner — a `make
  eval-mm` target / flag-gated pytest and the CI workflow that invokes it with
  the flag and key set. The item includes a caption-only baseline that must FAIL
  when the image part is withheld (proving pixels, not caption); per the
  acceptance criteria that baseline ships as a standalone pytest so the shared
  `ask()`/`run()` signatures stay unchanged. Existing text-only mean score is
  unchanged.
- **Rollout:** behind `MULTIMODAL_ENABLED` (default off); additive
  `schema_version`; no `documents` migration; binary store via a mounted
  `MEDIA_DIR` volume. Disable = exact text-only behavior. Only flip the flag /
  promote past `Backlog` after specs 02 and 03 are merged.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Canonical document model](../../CANONICAL_MODEL.md) (see *Deferred → Multi-modal blocks*)
- [Canonical document model spec](../02-canonical-document-model/README.md)
- [Real layout backends spec](../03-real-layout-backends/README.md)

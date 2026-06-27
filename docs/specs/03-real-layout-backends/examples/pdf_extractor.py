"""ILLUSTRATIVE — proposed PdfExtractor for app/layout.py. NOT wired in.

The load-bearing backend (spec 03 §1 of design.md). Single-column, text-layer
PDFs only. Emits the spec-02 canonical Document with:
  - one Block per reconstructed text block, carrying Locator(page, bbox)
  - footnote definitions as Block(type=footnote)
  - citations linked by glyph marker via Relation(type=footnote_ref) -- NO inline
    [^id] token is synthesized; the relation is the only linkage carrier.
  - planted distractors (page numbers, running header/footer, line numbers)
    rejected so they never become footnote blocks or links.

Geometry API is pypdfium2 (Apache/BSD). pdfium coords: origin BOTTOM-left, y UP.

Real seam this plugs into (app/layout.py):
    class LayoutExtractor(Protocol):
        def extract(self, raw: str | bytes, source: str) -> Document: ...
    register("pdf", PdfExtractor())

Canonical types (owned by spec 02; imported from app.layout per its seam):
    Document(schema_version, source, format, meta, blocks, relations)
    Block(id, type, text, level=0, order=None, locator=None, attrs={})
    Locator(page=None, bbox=None, char_start=None, char_end=None, path=None)
    Relation(type, from_id, to_id)
    BlockType: heading|paragraph|...|footnote|record|...
    SCHEMA_VERSION: str

Do NOT import this file. Port the pieces into app/layout.py.
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c  # raw C API: FPDFText_GetFontSize is not wrapped

# In the real module these come from the canonical types spec 02 adds:
from app.layout import (  # noqa: F401  (illustrative import path; see examples/README)
    SCHEMA_VERSION,
    Block,
    Document,
    Locator,
    Relation,
)

log = logging.getLogger(__name__)

# Footnote/citation marker alphabet. Superscripts are normalized to ASCII.
# NOTE: source and target MUST be in matching digit order (⁰→0 … ⁹→9). An earlier
# draft used "¹²³⁰⁴⁵⁶⁷⁸⁹"→"1234567890", which silently mapped ⁰→4, ⁴→5 … ⁹→0 and
# broke marker matching for any footnote numbered 0 or 4–9 — a direct recall miss
# against the 100%-recall acceptance bar.
_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹",
                       "0123456789")
# 1..999 | one or more of * † ‡ (multi-symbol markers like "††" per design §7)
_MARKER = re.compile(r"^[\s]*([0-9]{1,3}|[*†‡]+)")


@dataclass
class _Glyph:
    ch: str
    l: float
    b: float
    r: float
    t: float
    size: float


def _normalize_marker(s: str) -> str | None:
    m = _MARKER.match(s.translate(_SUPER))
    return m.group(1) if m else None


class PdfExtractor:
    """pypdfium2-backed PDF -> canonical Document (single-column, text-layer)."""

    SMALL = 0.9        # definition font < SMALL * body median
    SUPER = 0.8        # citation font < SUPER * body median
    RAISE = 0.2        # citation baseline raised >= RAISE * line height

    def extract(self, raw: str | bytes, source: str) -> Document:
        if isinstance(raw, str):  # binary backend: must receive bytes (see OQ5)
            raise TypeError("PdfExtractor expects raw bytes, not str")
        pdf = pdfium.PdfDocument(raw)

        pages = [self._read_page(pdf, i) for i in range(len(pdf))]
        boilerplate = self._boilerplate(pages)  # text repeated across pages

        blocks: list[Block] = []
        relations: list[Relation] = []
        bid = 0

        for pno, (glyphs, width, height) in enumerate(pages, start=1):
            if not glyphs:
                continue  # no text layer on this page (scanned) -- degrade, don't OCR
            body_size = statistics.median(g.size for g in glyphs)
            lines = self._lines(glyphs, body_size)
            line_blocks = self._blocks(lines, body_size)

            defs: dict[str, Block] = {}
            cites: list[tuple[Block, str]] = []
            y_foot = self._footer_band(line_blocks, body_size)

            for lb in line_blocks:
                text = lb["text"].strip()
                if not text or self._is_boilerplate(text, lb, boilerplate, height):
                    continue  # running header/footer/page-number/line-number furniture
                bid += 1
                blk = Block(
                    id=f"b{bid}", type=self._block_type(lb, body_size), text=text,
                    locator=Locator(page=pno, bbox=lb["bbox"]),
                )
                blocks.append(blk)

                if self._is_definition(lb, body_size, y_foot):
                    blk.type = "footnote"
                    marker = _normalize_marker(text)
                    if marker:
                        blk.attrs = {"marker": marker}
                        defs[marker] = blk
                else:
                    for marker in self._citation_markers(lb, body_size):
                        cites.append((blk, marker))

            # per-page link: citation -> same-page definition (design.md §1.6)
            for citing, marker in cites:
                if marker in defs:
                    relations.append(Relation("footnote_ref", citing.id, defs[marker].id))
                # else: unlinkable citation -- leave glyph in text, emit no relation
            # definitions with no incoming citation stay as footnote blocks with no
            # inbound footnote_ref == v0 orphan behavior (trailing chunk downstream).

        if not blocks:
            log.warning("pdf %s: no text layer on any page (scanned?); emitting empty doc", source)
        return Document(
            schema_version=SCHEMA_VERSION, source=source, format="pdf",
            meta={"pages": len(pages)}, blocks=blocks, relations=relations,
        )

    # --- geometry ----------------------------------------------------------

    def _read_page(self, pdf, i: int):
        page = pdf[i]
        width, height = page.get_size()
        tp = page.get_textpage()
        glyphs = []
        for j in range(tp.count_chars()):
            ch = tp.get_text_range(j, 1)
            if not ch.strip():
                continue
            l, b, r, t = tp.get_charbox(j)
            size = pdfium_c.FPDFText_GetFontSize(tp.raw, j)
            glyphs.append(_Glyph(ch, l, b, r, t, size))
        return glyphs, width, height

    def _lines(self, glyphs: list[_Glyph], body: float) -> list[list[_Glyph]]:
        tol = 0.3 * body
        out: list[list[_Glyph]] = []
        for g in sorted(glyphs, key=lambda g: (-g.b, g.l)):
            if out and abs(out[-1][-1].b - g.b) <= tol:
                out[-1].append(g)
            else:
                out.append([g])
        for line in out:
            line.sort(key=lambda g: g.l)
        return out

    def _blocks(self, lines: list[list[_Glyph]], body: float) -> list[dict]:
        blocks: list[dict] = []
        for line in lines:
            text = "".join(g.ch for g in line)
            box = [min(g.l for g in line), min(g.b for g in line),
                   max(g.r for g in line), max(g.t for g in line)]
            size = statistics.median(g.size for g in line)
            top = max(g.t for g in line)
            if blocks and (blocks[-1]["bbox"][1] - top) <= 1.5 * body \
                    and abs(blocks[-1]["bbox"][0] - box[0]) <= body:
                prev = blocks[-1]
                prev["text"] += " " + text
                prev["bbox"] = [min(prev["bbox"][0], box[0]), min(prev["bbox"][1], box[1]),
                                max(prev["bbox"][2], box[2]), max(prev["bbox"][3], box[3])]
                prev["lines"].append(line)
            else:
                blocks.append({"text": text, "bbox": box, "size": size, "lines": [line]})
        return blocks

    def _footer_band(self, blocks: list[dict], body: float) -> float:
        body_lines = [b for b in blocks if b["size"] >= self.SMALL * body]
        if not body_lines:
            return 0.0
        return min(b["bbox"][1] for b in body_lines) - 0.5 * body

    # --- classification ----------------------------------------------------

    def _block_type(self, lb: dict, body: float) -> str:
        # OQ3: heading inference is deferrable; v1 may return "paragraph" always.
        if lb["size"] > 1.15 * body and len(lb["text"].split()) <= 12:
            return "heading"
        return "paragraph"

    def _is_definition(self, lb: dict, body: float, y_foot: float) -> bool:
        return (lb["bbox"][3] <= y_foot
                and lb["size"] < self.SMALL * body
                and _normalize_marker(lb["text"]) is not None)

    def _citation_markers(self, lb: dict, body: float) -> list[str]:
        markers: list[str] = []
        # Raise is measured against the BLOCK's body baseline, not the per-line
        # baseline: a superscript can be split into its own one-glyph line by
        # _lines() (design.md §1.4 / README OQ7), and a lone raised glyph has no
        # body baseline of its own to be "raised" above.
        body_glyphs = [g for line in lb["lines"] for g in line
                       if g.size >= self.SMALL * body]
        block_base = statistics.median(g.b for g in body_glyphs) if body_glyphs \
            else statistics.median(g.b for line in lb["lines"] for g in line)
        for line in lb["lines"]:
            base = block_base
            height = statistics.median(g.t - g.b for g in line) or body
            run = ""
            for g in line:
                raised = (g.b - base) >= self.RAISE * height
                small = g.size < self.SUPER * body
                if small and raised and g.ch.strip():
                    run += g.ch
                elif run:
                    if (m := _normalize_marker(run)):
                        markers.append(m)
                    run = ""
            if run and (m := _normalize_marker(run)):
                markers.append(m)
        return markers

    # --- false-positive rejection (design.md §1.7) -------------------------

    def _boilerplate(self, pages) -> set[str]:
        from collections import Counter
        seen: Counter[tuple] = Counter()
        for glyphs, _w, h in pages:
            for line in self._lines(glyphs, statistics.median([g.size for g in glyphs] or [1])):
                text = "".join(g.ch for g in line).strip()
                yband = round(min(g.b for g in line) / 10) if line else 0  # coarse y bucket
                if text:
                    seen[(text, yband)] += 1
        threshold = max(2, (len(pages) + 1) // 2)
        return {t for (t, _y), n in seen.items() if n >= threshold}

    def _is_boilerplate(self, text: str, lb: dict, boilerplate: set[str], height: float) -> bool:
        if text in boilerplate:
            return True  # running header/footer repeated across pages
        # bare page number: a single short numeric token low or high on the page
        if re.fullmatch(r"\d{1,4}", text) and (
                lb["bbox"][3] < 0.12 * height or lb["bbox"][1] > 0.88 * height):
            return True
        return False

"""Layout extraction: turn a layout-rich document into ordered elements plus a
footnote table, so chunking can respect structure and keep each footnote with the
text that cites it.

Pluggable by design: `register(fmt, extractor)` swaps in a heavier backend
(docling / pymupdf / python-docx / openpyxl) without touching the router or the
chunker. Every extractor returns the same `LayoutDoc`, and normalizes inline
footnote references to a single `[^id]` marker in element text so downstream
chunking is format-agnostic.

Bundled defaults are dependency-free: Markdown, HTML, and CSV. Formats that
genuinely need a parser (PDF, DOCX, XLSX) ship as register-a-backend stubs.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol


@dataclass
class Element:
    kind: str  # heading | body | table | record
    text: str  # inline footnote refs normalized to "[^id]" markers
    level: int = 0  # heading depth 1..6; 0 otherwise
    footnote_ids: list[str] = field(default_factory=list)  # ids cited in this element
    meta: dict = field(default_factory=dict)


@dataclass
class LayoutDoc:
    source: str
    elements: list[Element]
    footnotes: dict[str, str] = field(default_factory=dict)  # id -> definition text
    meta: dict = field(default_factory=dict)


class LayoutExtractor(Protocol):
    def extract(self, raw: str | bytes, source: str) -> LayoutDoc: ...


# --- helpers ---------------------------------------------------------------

def _text(raw: str | bytes) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _norm_fn(s: str) -> str:
    """Normalize an HTML footnote id/href (#fn1, fnref1, fn1) to a bare key."""
    s = s.lstrip("#")
    s = re.sub(r"^fnref", "", s)
    s = re.sub(r"^fn", "", s)
    return s or "0"


# --- Markdown --------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_FN_DEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
_FN_REF = re.compile(r"\[\^([^\]]+)\]")


class MarkdownExtractor:
    def extract(self, raw: str | bytes, source: str) -> LayoutDoc:
        lines = _text(raw).splitlines()
        footnotes: dict[str, str] = {}

        # Pass 1: pull footnote definitions (with indented continuation lines).
        body: list[str] = []
        i = 0
        while i < len(lines):
            m = _MD_FN_DEF.match(lines[i])
            if m:
                fid, deftext = m.group(1), m.group(2).strip()
                j = i + 1
                while j < len(lines) and lines[j][:1] in (" ", "\t") and lines[j].strip():
                    deftext += " " + lines[j].strip()
                    j += 1
                footnotes[fid] = deftext
                i = j
            else:
                body.append(lines[i])
                i += 1

        # Pass 2: blocks. Blank line or heading ends a block; tables stay whole.
        elements: list[Element] = []
        buf: list[str] = []

        def flush():
            if not buf:
                return
            block = "\n".join(buf).strip()
            buf.clear()
            if not block:
                return
            refs = _dedupe(_FN_REF.findall(block))
            kind = "table" if re.search(r"^\s*\|.*\|", block, re.M) else "body"
            elements.append(Element(kind, block, footnote_ids=refs))

        for ln in body:
            h = _MD_HEADING.match(ln)
            if h:
                flush()
                title = h.group(2).strip()
                elements.append(
                    Element("heading", title, level=len(h.group(1)),
                            footnote_ids=_dedupe(_FN_REF.findall(title)))
                )
            elif ln.strip() == "":
                flush()
            else:
                buf.append(ln)
        flush()
        return LayoutDoc(source, elements, footnotes, {"format": "markdown"})


# --- HTML ------------------------------------------------------------------

class _HtmlLayout(HTMLParser):
    BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.footnotes: dict[str, str] = {}
        self._capture = False
        self._tag = None
        self._level = 0
        self._buf: list[str] = []
        self._ids: list[str] = []
        self._block_id = None
        self._in_ref = False  # inside a footnote-reference <a>, skip its marker text
        self._table: list[str] | None = None  # not None => capturing a <table>

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._table is not None:
            if tag == "tr":
                self._table.append("\n")
            elif tag in ("td", "th"):
                self._table.append(" | ")
            return
        if tag == "table":
            self._flush()
            self._table = []
        elif tag in self.BLOCK and not self._capture:
            # First block tag wins; nested block tags (e.g. <li><p>...) merge into it.
            self._capture = True
            self._tag = tag
            self._level = int(tag[1]) if tag[0] == "h" and tag[1:].isdigit() else 0
            self._block_id = a.get("id")
        elif tag == "a" and self._capture:
            href = a.get("href", "")
            # A link into the footnotes section ("#fn1") is a citation; a back-link
            # out of a definition ("#fnref1") is not.
            if href.startswith("#fn") and not href.startswith("#fnref"):
                fid = _norm_fn(href)
                self._ids.append(fid)
                self._buf.append(f"[^{fid}]")
                self._in_ref = True
        elif tag == "br" and self._capture:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if self._table is not None:
            if tag == "table":
                text = re.sub(r"[ \t]+", " ", "".join(self._table)).strip()
                self._table = None
                if text:
                    self.elements.append(Element("table", text))
            return
        if tag == "a":
            self._in_ref = False
        elif tag in self.BLOCK and tag == self._tag:
            self._flush()

    def handle_data(self, data):
        if self._table is not None:
            self._table.append(data)
        elif self._capture and not self._in_ref:
            self._buf.append(data)

    def _flush(self):
        if not self._capture:
            return
        text = re.sub(r"[ \t]+", " ", "".join(self._buf)).strip()
        level, ids, bid = self._level, _dedupe(self._ids), self._block_id
        self._capture = False
        self._tag = self._block_id = None
        self._level = 0
        self._buf, self._ids = [], []
        if not text:
            return
        if bid and bid.startswith("fn") and not bid.startswith("fnref"):
            # footnote definition (e.g. <li id="fn1">...<a href="#fnref1">↩</a></li>)
            self.footnotes[_norm_fn(bid)] = re.sub(r"\s*[↩↩].*$", "", text).strip()
            return
        kind = "heading" if level else "body"
        self.elements.append(Element(kind, text, level=level, footnote_ids=ids))


class HtmlExtractor:
    def extract(self, raw: str | bytes, source: str) -> LayoutDoc:
        p = _HtmlLayout()
        p.feed(_text(raw))
        p._flush()
        return LayoutDoc(source, p.elements, p.footnotes, {"format": "html"})


# --- CSV (tabular: records, no footnotes) ----------------------------------

class CsvExtractor:
    def extract(self, raw: str | bytes, source: str) -> LayoutDoc:
        rows = list(csv.reader(io.StringIO(_text(raw))))
        if not rows:
            return LayoutDoc(source, [], {}, {"format": "csv"})
        header = rows[0]
        elements = [
            Element("record", " | ".join(f"{h}: {v}" for h, v in zip(header, r)),
                    meta={"row": n})
            for n, r in enumerate(rows[1:], start=1)
        ]
        return LayoutDoc(source, elements, {}, {"format": "csv", "columns": header})


# --- register-a-backend stubs (heavy formats) ------------------------------

class _RequiresBackend:
    """Placeholder for a format with no dependency-free default. Register a real
    extractor to enable it: `layout.register("pdf", MyPdfExtractor())`."""

    def __init__(self, fmt: str, libs: str):
        self.fmt, self.libs = fmt, libs

    def extract(self, raw, source):
        raise NotImplementedError(
            f"No bundled extractor for .{self.fmt}. Register a backend that returns "
            f"a layout.LayoutDoc -- recommended: {self.libs}. "
            f"e.g. `from app import layout; layout.register('{self.fmt}', MyExtractor())`."
        )


# --- registry --------------------------------------------------------------

_ALIASES = {"md": "markdown", "markdown": "markdown", "htm": "html", "html": "html",
            "csv": "csv", "pdf": "pdf", "docx": "docx", "xlsx": "xlsx"}

_EXTRACTORS: dict[str, LayoutExtractor] = {}


def register(fmt: str, extractor: LayoutExtractor) -> None:
    _EXTRACTORS[_ALIASES.get(fmt, fmt)] = extractor


def get_extractor(fmt: str) -> LayoutExtractor:
    key = _ALIASES.get(fmt.lower(), fmt.lower())
    if key not in _EXTRACTORS:
        raise KeyError(f"no layout extractor registered for format {fmt!r}")
    return _EXTRACTORS[key]


def canonical(fmt: str) -> str:
    return _ALIASES.get(fmt.lower(), fmt.lower())


register("markdown", MarkdownExtractor())
register("html", HtmlExtractor())
register("csv", CsvExtractor())
register("pdf", _RequiresBackend("pdf", "docling or pymupdf (fitz)"))
register("docx", _RequiresBackend("docx", "python-docx or docling"))
register("xlsx", _RequiresBackend("xlsx", "openpyxl"))

TABULAR = {"csv", "xlsx"}

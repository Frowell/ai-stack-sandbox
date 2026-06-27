"""ILLUSTRATIVE ONLY — spec, not wired-in code. See examples/README.md.

Would become the v1 canonical types in `app/layout.py` (which PR #2 introduces;
it does NOT exist in the tree yet). Mirrors the contract in
`docs/CANONICAL_MODEL.md`. Stdlib dataclasses + enum only — no pydantic — because
this is an internal in-process contract and the repo has no pydantic dep today
(see pyproject.toml).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

SCHEMA_VERSION = "1"  # single source of truth; stamped by extractors, asserted by readers


class BlockType(str, Enum):
    """Closed taxonomy. Backends map INTO this set; they never invent kinds."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CAPTION = "caption"
    CODE = "code"
    BLOCKQUOTE = "blockquote"
    FIGURE = "figure"
    FOOTNOTE = "footnote"
    RECORD = "record"
    PAGE_BREAK = "page_break"


class RelationType(str, Enum):
    FOOTNOTE_REF = "footnote_ref"  # the only one v1 is required to PRODUCE
    CAPTION_OF = "caption_of"      # reserved (schema-valid) but deferred
    CONTAINS = "contains"          # reserved
    CROSS_REF = "cross_ref"        # reserved


@dataclass(frozen=True)
class Locator:
    """All optional. md/html/csv omit this entirely; PR #3 (PDF) is the first
    real producer (and decides the char_start/char_end base — see README)."""

    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    char_start: int | None = None
    char_end: int | None = None
    path: str | None = None  # breadcrumb / DOM path, e.g. "Mature Stacks > Observability"


@dataclass
class Block:
    id: str
    type: BlockType
    text: str
    level: int | None = None
    order: int | None = None  # advisory; if present MUST equal array index (chunker ignores it)
    locator: Locator | None = None
    attrs: dict = field(default_factory=dict)  # e.g. {"marker": "otel"} or table {"columns": [...]}

    def __post_init__(self) -> None:
        # Closed-taxonomy is ENFORCED, not advisory: an unmapped kind fails loudly
        # here at extraction, not silently three modules downstream.
        if not isinstance(self.type, BlockType):
            try:
                object.__setattr__(self, "type", BlockType(self.type))
            except ValueError as e:
                raise ValueError(f"unknown BlockType {self.type!r}; not in closed set") from e


@dataclass
class Relation:
    type: RelationType
    from_id: str
    to_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.type, RelationType):
            self.type = RelationType(self.type)  # raises ValueError on unknown


@dataclass
class Document:
    source: str
    format: str  # "markdown" | "html" | "csv" | "pdf" | ...
    blocks: list[Block]
    relations: list[Relation] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


# --- v0 -> v1 mapping (lives in each extractor) --------------------------------
# PR #2's v0 Element.kind strings -> v1 BlockType. Backends map INTO the set.
V0_KIND_TO_BLOCKTYPE: dict[str, BlockType] = {
    "heading": BlockType.HEADING,
    "body": BlockType.PARAGRAPH,   # the rename
    "table": BlockType.TABLE,
    "record": BlockType.RECORD,
    # NB: the v0 orphan "notes" chunk is mapped to a FOOTNOTE block with NO
    # inbound footnote_ref (handled in the extractor below), not via this table.
}


def map_v0_kind(kind: str) -> BlockType:
    """Raise on any v0 kind not in the mapping (closed taxonomy, fail loud)."""
    try:
        return V0_KIND_TO_BLOCKTYPE[kind]
    except KeyError as e:
        raise ValueError(f"unmapped v0 kind {kind!r}; add to V0_KIND_TO_BLOCKTYPE or fix the backend") from e


# --- version guard (asserted at adapter + stored-manifest boundaries) ----------
def assert_supported_version(schema_version: str) -> None:
    """Reject an unknown MAJOR rather than mis-parse. Same-major is accepted."""
    incoming_major = schema_version.split(".", 1)[0]
    current_major = SCHEMA_VERSION.split(".", 1)[0]
    if incoming_major != current_major:
        raise ValueError(
            f"unsupported canonical schema_version {schema_version!r} "
            f"(reader supports major {current_major!r})"
        )


# --- illustrative md extractor (omits locator/order; never fakes them) ---------
def extract_markdown(source: str, elements: list[dict]) -> Document:
    """`elements` is PR #2's v0 LayoutDoc.elements shape: {kind, text, level?, ...}.
    Optional fields (locator, real order) are OMITTED for token-only formats."""
    blocks = []
    for i, el in enumerate(elements):
        kind = el["kind"]
        # The orphan v0 `notes` chunk is special-cased HERE (not via the mapping
        # table): it becomes a FOOTNOTE block with no inbound footnote_ref, so the
        # chunker drops it into the trailing uncited chunk. Everything else maps
        # through the closed table and an unmapped kind RAISES.
        block_type = BlockType.FOOTNOTE if kind == "notes" else map_v0_kind(kind)
        blocks.append(
            Block(
                id=f"b{i}",
                type=block_type,
                text=el["text"],
                level=el.get("level"),
                # locator and order intentionally omitted: md has no geometry and the
                # array index IS the reading order.
            )
        )
    return Document(source=source, format="markdown", blocks=blocks)

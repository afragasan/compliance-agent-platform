"""Parser for the regulatory-corpus text format.

Each source file in ``docs/regulatory-corpus/`` is a sequence of blank-line-separated
records, one per pre-defined chunk:

    [CHUNK_ID]: OFAC-014
    [REGULATORY_REF]: OFAC Guidance on True Match Escalation
    [TOPIC]: True Match Freeze Protocol & Escrow Placement
    [COMPLIANCE_LEVEL]: Mandatory
    [TEXT]: Upon confirming a True Match against an SDN target, ...
    [CITATION]: OFAC True Match Handling & Property Freeze Manual (31 CFR 501.308)

The corpus arrives already chunked at a sensible granularity (one numbered clause per
record), so this module parses records rather than splitting prose — there is no
sliding-window/token-based chunker here because the real corpus never needs one.
"""

from __future__ import annotations

import re
from pathlib import Path

from compliance_agent_platform.schemas.retrieval import DocumentChunk, DocumentMetadata

_TAG_LINE = re.compile(r"^\[([A-Z_]+)\]:\s?(.*)$")


def _parse_block(block: str) -> dict[str, str]:
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in block.splitlines():
        match = _TAG_LINE.match(line)
        if match:
            current = match.group(1)
            fields[current] = [match.group(2)]
        elif current is not None:
            fields[current].append(line)
    return {tag: "\n".join(lines).strip() for tag, lines in fields.items()}


def parse_corpus_file(path: Path) -> list[DocumentChunk]:
    """Parse one corpus file into its :class:`DocumentChunk` records.

    ``document_id`` is the file's stem; ``program`` is inferred from the
    ``CHUNK_ID`` prefix (e.g. ``"OFAC-014"`` -> ``"OFAC"``), so no filename
    convention beyond "one file per source document" is required.
    """
    document_id = path.stem
    # NUL bytes are valid UTF-8 but Postgres text/JSONB columns reject them outright;
    # the supplied corpus files each carry one stray trailing NUL with no textual
    # meaning, so drop any rather than let a downstream pgvector insert fail on it.
    text = path.read_text(encoding="utf-8").replace("\x00", "")
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]

    chunks: list[DocumentChunk] = []
    for index, block in enumerate(blocks):
        record = _parse_block(block)
        chunk_id = record.get("CHUNK_ID")
        body = record.get("TEXT")
        if not chunk_id or not body:
            continue  # malformed record - skip rather than fail the whole file
        program = chunk_id.split("-")[0]
        metadata = DocumentMetadata(
            document_id=document_id,
            title=record.get("TOPIC", chunk_id),
            program=program,
            section=record.get("REGULATORY_REF") or None,
            citation=record.get("CITATION") or None,
            compliance_level=record.get("COMPLIANCE_LEVEL") or None,
        )
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                document_id=document_id,
                chunk_index=index,
                text=body,
                metadata=metadata,
            )
        )
    return chunks

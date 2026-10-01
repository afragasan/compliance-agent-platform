"""``rag.corpus.parse_corpus_file`` against both synthetic fixtures and the real
supplied regulatory-corpus files."""

from __future__ import annotations

from pathlib import Path

from compliance_agent_platform.rag.corpus import parse_corpus_file

_CORPUS_DIR = Path(__file__).resolve().parent.parent / "docs" / "regulatory-corpus"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_parses_a_single_well_formed_block(tmp_path):
    path = _write(
        tmp_path,
        "sample-corpus.txt",
        "[CHUNK_ID]: TEST-001\n"
        "[REGULATORY_REF]: Test Ref\n"
        "[TOPIC]: Test Topic\n"
        "[COMPLIANCE_LEVEL]: Mandatory\n"
        "[TEXT]: This is the chunk body.\n"
        "[CITATION]: Test Citation\n",
    )
    chunks = parse_corpus_file(path)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_id == "TEST-001"
    assert chunk.document_id == "sample-corpus"
    assert chunk.chunk_index == 0
    assert chunk.text == "This is the chunk body."
    assert chunk.metadata.program == "TEST"
    assert chunk.metadata.title == "Test Topic"
    assert chunk.metadata.section == "Test Ref"
    assert chunk.metadata.citation == "Test Citation"
    assert chunk.metadata.compliance_level == "Mandatory"


def test_parses_multiple_blank_line_separated_blocks(tmp_path):
    path = _write(
        tmp_path,
        "sample-corpus.txt",
        "[CHUNK_ID]: TEST-001\n[TEXT]: First body.\n\n[CHUNK_ID]: TEST-002\n[TEXT]: Second body.\n",
    )
    chunks = parse_corpus_file(path)

    assert [c.chunk_id for c in chunks] == ["TEST-001", "TEST-002"]
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_skips_malformed_blocks_missing_required_tags(tmp_path):
    path = _write(
        tmp_path,
        "sample-corpus.txt",
        "[TOPIC]: no chunk id or text here\n\n[CHUNK_ID]: TEST-002\n[TEXT]: valid body.\n",
    )
    chunks = parse_corpus_file(path)

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "TEST-002"


def test_multiline_text_field_is_joined(tmp_path):
    path = _write(
        tmp_path,
        "sample-corpus.txt",
        "[CHUNK_ID]: TEST-001\n[TEXT]: First line of the body.\nSecond line of the body.\n",
    )
    chunks = parse_corpus_file(path)

    assert chunks[0].text == "First line of the body.\nSecond line of the body."


def test_strips_stray_nul_bytes(tmp_path):
    # Postgres text/JSONB columns reject NUL bytes outright; the real corpus files
    # each carry one stray trailing NUL with no textual meaning (see rag/corpus.py).
    path = _write(
        tmp_path, "sample-corpus.txt", "[CHUNK_ID]: TEST-001\n[TEXT]: Body.\n[CITATION]: C.\x00"
    )
    chunks = parse_corpus_file(path)

    assert "\x00" not in chunks[0].metadata.citation


def test_real_corpus_files_all_parse_and_are_program_tagged():
    files = sorted(_CORPUS_DIR.glob("*.txt"))
    assert files, f"expected real corpus files under {_CORPUS_DIR}"

    all_chunk_ids: set[str] = set()
    for path in files:
        chunks = parse_corpus_file(path)
        assert chunks, f"{path.name} produced no chunks"
        for chunk in chunks:
            assert chunk.chunk_id not in all_chunk_ids, f"duplicate chunk_id {chunk.chunk_id}"
            all_chunk_ids.add(chunk.chunk_id)
            assert chunk.text
            assert chunk.metadata.program == chunk.chunk_id.split("-")[0]
            assert chunk.document_id == path.stem

    programs = {cid.split("-")[0] for cid in all_chunk_ids}
    assert programs == {"OFAC", "NACHA", "MTRA"}

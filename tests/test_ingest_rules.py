"""Tests for ingest chunk assembly and caption merge."""

from __future__ import annotations

from roboto_guilliman.ingestion.ingest_rules import (
    IngestChunk,
    apply_page_captions,
    chunk_doc_id,
    chunk_to_payload,
    summarize_ingest,
)


def _sample_chunk(*, page: int = 10, rule_number: str = "01.03") -> IngestChunk:
    return IngestChunk(
        text=f"Rule {rule_number} body",
        page=page,
        chunk_index=0,
        parser_profile="core_rules",
        chunk_type="core_rule",
        source="core_rules_11th",
        source_category="new40k",
        rule_number=rule_number,
        parent_section="EXAMPLE RULE",
    )


def test_apply_page_captions_merges_by_page():
    chunks = [
        _sample_chunk(page=10, rule_number="01.03"),
        _sample_chunk(page=31, rule_number="04.01"),
    ]
    merged = apply_page_captions(chunks, {31: "Battle-shock diagram caption."})

    assert merged[0].has_figure is False
    assert merged[0].figure_description is None
    assert merged[1].has_figure is True
    assert merged[1].figure_description == "Battle-shock diagram caption."


def test_chunk_doc_id_uses_rule_number_for_core_rules():
    chunk = _sample_chunk(rule_number="12.07")
    assert chunk_doc_id("core_rules_11th", chunk) == chunk_doc_id(
        "core_rules_11th",
        _sample_chunk(rule_number="12.07"),
    )
    assert chunk_doc_id("core_rules_11th", chunk) != chunk_doc_id(
        "core_rules_11th",
        _sample_chunk(rule_number="12.08"),
    )


def test_chunk_to_payload_includes_figure_fields():
    chunk = apply_page_captions(
        [_sample_chunk(page=31, rule_number="04.01")],
        {31: "Diagram caption."},
    )[0]
    payload = chunk_to_payload(chunk, [0.1, 0.2, 0.3])

    assert payload["has_figure"] is True
    assert payload["figure_description"] == "Diagram caption."
    assert payload["rule_number"] == "04.01"
    assert payload["parser_profile"] == "core_rules"


def test_summarize_ingest_counts_figure_chunks():
    chunks = apply_page_captions(
        [
            _sample_chunk(page=10, rule_number="01.01"),
            _sample_chunk(page=31, rule_number="04.01"),
            _sample_chunk(page=31, rule_number="04.02"),
        ],
        {31: "Shared page caption for both rules on page 31."},
    )
    stats = summarize_ingest(chunks)

    assert stats["chunks"] == 3
    assert stats["figure_chunks"] == 2
    assert stats["figure_pages"] == 1

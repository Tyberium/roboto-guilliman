"""Parse rulebook PDFs and ingest chunked embeddings into Firestore."""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

from roboto_guilliman.chunking import TextChunk, chunk_page_text
from roboto_guilliman.config import Settings, get_settings
from roboto_guilliman.embeddings import EmbeddingService
from roboto_guilliman.ingestion.caption_pages import (
    default_core_rules_pdf,
    load_page_captions,
    sha256_file,
)
from roboto_guilliman.gcp_auth import optional_local_credentials
from roboto_guilliman.ingestion.parsers.core_rules import CoreRuleChunk, parse_core_rules_pdf
from roboto_guilliman.ingestion.source_registry import (
    ParserProfile,
    assert_ingestible_pdf,
)

logger = logging.getLogger(__name__)

SOURCE_CATEGORY_BY_PROFILE: dict[ParserProfile, str] = {
    ParserProfile.CORE_RULES: "new40k",
    ParserProfile.UPDATES_AND_FAQ: "core-rules-and-key-downloads",
    ParserProfile.REFERENCE: "core-rules-and-key-downloads",
    ParserProfile.FACTION_PACKS: "faction-packs",
    ParserProfile.EVENT_COMPANIONS: "event-companions",
    ParserProfile.MISCELLANEOUS: "miscellaneous",
}


@dataclass(frozen=True)
class IngestChunk:
    text: str
    page: int
    chunk_index: int
    parser_profile: str
    chunk_type: str
    source: str
    source_category: str
    rule_number: str | None = None
    parent_section: str | None = None
    section_hint: str | None = None
    has_figure: bool = False
    figure_description: str | None = None


def extract_recursive_chunks(
    pdf_path: Path,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[TextChunk]:
    doc = fitz.open(pdf_path)
    chunks: list[TextChunk] = []
    chunk_index = 0
    try:
        for page_number, page in enumerate(doc, start=1):
            page_text = page.get_text("text")
            if not page_text.strip():
                continue
            page_chunks = chunk_page_text(
                page_text,
                page_number=page_number,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                start_index=chunk_index,
            )
            chunks.extend(page_chunks)
            chunk_index += len(page_chunks)
    finally:
        doc.close()
    return chunks


def core_rule_to_ingest_chunk(chunk: CoreRuleChunk, *, source: str, source_category: str) -> IngestChunk:
    return IngestChunk(
        text=chunk.text,
        page=chunk.page,
        chunk_index=chunk.chunk_index,
        parser_profile=ParserProfile.CORE_RULES,
        chunk_type=chunk.chunk_type,
        source=source,
        source_category=source_category,
        rule_number=chunk.rule_number,
        parent_section=chunk.title,
    )


def recursive_to_ingest_chunk(
    chunk: TextChunk,
    *,
    source: str,
    parser_profile: ParserProfile,
    source_category: str,
) -> IngestChunk:
    return IngestChunk(
        text=chunk.text,
        page=chunk.page,
        chunk_index=chunk.chunk_index,
        parser_profile=parser_profile,
        chunk_type="miscellaneous",
        source=source,
        source_category=source_category,
        section_hint=chunk.section_hint,
    )


def apply_page_captions(
    chunks: list[IngestChunk],
    captions_by_page: dict[int, str],
) -> list[IngestChunk]:
    if not captions_by_page:
        return chunks
    return [
        IngestChunk(
            text=chunk.text,
            page=chunk.page,
            chunk_index=chunk.chunk_index,
            parser_profile=chunk.parser_profile,
            chunk_type=chunk.chunk_type,
            source=chunk.source,
            source_category=chunk.source_category,
            rule_number=chunk.rule_number,
            parent_section=chunk.parent_section,
            section_hint=chunk.section_hint,
            has_figure=chunk.page in captions_by_page,
            figure_description=captions_by_page.get(chunk.page),
        )
        for chunk in chunks
    ]


def extract_ingest_chunks(
    pdf_path: Path,
    *,
    parser_profile: ParserProfile,
    source: str,
    chunk_size: int,
    chunk_overlap: int,
    captions_by_page: dict[int, str] | None = None,
) -> list[IngestChunk]:
    source_category = SOURCE_CATEGORY_BY_PROFILE.get(parser_profile, "miscellaneous")

    if parser_profile == ParserProfile.CORE_RULES:
        parsed = parse_core_rules_pdf(pdf_path)
        chunks = [
            core_rule_to_ingest_chunk(chunk, source=source, source_category=source_category)
            for chunk in parsed
        ]
    else:
        parsed = extract_recursive_chunks(
            pdf_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        chunks = [
            recursive_to_ingest_chunk(
                chunk,
                source=source,
                parser_profile=parser_profile,
                source_category=source_category,
            )
            for chunk in parsed
        ]

    if captions_by_page:
        chunks = apply_page_captions(chunks, captions_by_page)
    return chunks


def chunk_doc_id(source: str, chunk: IngestChunk) -> str:
    if chunk.rule_number:
        key = f"{source}:{chunk.rule_number}"
    else:
        key = f"{source}:{chunk.page}:{chunk.chunk_index}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def chunk_to_payload(chunk: IngestChunk, vector: list[float]) -> dict[str, object]:
    payload: dict[str, object] = {
        "text": chunk.text,
        "embedding": Vector(vector),
        "parser_profile": chunk.parser_profile,
        "chunk_type": chunk.chunk_type,
        "source": chunk.source,
        "source_category": chunk.source_category,
        "page": chunk.page,
        "chunk_index": chunk.chunk_index,
        "has_figure": chunk.has_figure,
    }
    if chunk.rule_number is not None:
        payload["rule_number"] = chunk.rule_number
    if chunk.parent_section is not None:
        payload["parent_section"] = chunk.parent_section
    if chunk.section_hint is not None:
        payload["section_hint"] = chunk.section_hint
    if chunk.figure_description:
        payload["figure_description"] = chunk.figure_description
    return payload


def summarize_ingest(chunks: list[IngestChunk]) -> dict[str, int | float]:
    figure_chunks = [chunk for chunk in chunks if chunk.has_figure]
    pages_with_figures = {chunk.page for chunk in figure_chunks}
    text_chars = sum(len(chunk.text) for chunk in chunks)
    caption_chars = sum(len(chunk.figure_description or "") for chunk in figure_chunks)
    return {
        "chunks": len(chunks),
        "figure_chunks": len(figure_chunks),
        "figure_pages": len(pages_with_figures),
        "text_chars": text_chars,
        "caption_chars": caption_chars,
        "avg_text_chars": round(text_chars / len(chunks)) if chunks else 0,
    }


def ingest_to_firestore(
    chunks: list[IngestChunk],
    *,
    settings: Settings,
    batch_size: int = 16,
    dry_run: bool = False,
) -> int:
    if not chunks:
        logger.warning("No chunks to ingest.")
        return 0

    embedder = EmbeddingService(settings) if not dry_run else None
    credentials = optional_local_credentials()
    db = None if dry_run else firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
        credentials=credentials,
    )
    collection = None if dry_run else db.collection(settings.firestore_collection)
    written = 0

    for start in range(0, len(chunks), batch_size):
        batch_chunks = chunks[start : start + batch_size]
        if dry_run:
            vectors = [[0.0] * 768 for _ in batch_chunks]
        else:
            assert embedder is not None
            vectors = embedder.embed_documents([chunk.text for chunk in batch_chunks])
        batch = db.batch() if db is not None else None

        for chunk, vector in zip(batch_chunks, vectors, strict=True):
            doc_id = chunk_doc_id(chunk.source, chunk)
            payload = chunk_to_payload(chunk, vector)
            if dry_run:
                figure_note = " + figure" if chunk.has_figure else ""
                logger.info(
                    "Dry run: would write %s rule %s page %s%s",
                    doc_id,
                    chunk.rule_number or "-",
                    chunk.page,
                    figure_note,
                )
            else:
                assert batch is not None and collection is not None
                batch.set(collection.document(doc_id), payload)
            written += 1

        if batch is not None:
            batch.commit()
            logger.info("Committed batch %s-%s", start, start + len(batch_chunks) - 1)

    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest Warhammer 11th edition rule PDFs into Firestore vector search.",
    )
    parser.add_argument(
        "pdf_path",
        type=Path,
        nargs="?",
        help="Path to a rules PDF. Defaults to the #New40k core rules PDF.",
    )
    parser.add_argument(
        "--source-name",
        default="core_rules_11th",
        help="Logical source label stored on each chunk document.",
    )
    parser.add_argument(
        "--captions",
        type=Path,
        help="Override path to page_captions.json (default: beside the PDF).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report only; no embeddings or Firestore writes.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="Embedding/write batch size.",
    )
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()
    settings = get_settings()

    pdf_path = args.pdf_path or default_core_rules_pdf()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    parser_profile = assert_ingestible_pdf(pdf_path)
    captions_sha256, captions_by_page = load_page_captions(
        pdf_path,
        captions_path=args.captions,
    )
    if parser_profile == ParserProfile.CORE_RULES and not captions_by_page:
        logger.warning(
            "No page_captions.json found beside %s; ingesting without figure descriptions.",
            pdf_path,
        )
    elif captions_sha256:
        pdf_sha256 = sha256_file(pdf_path)
        if captions_sha256 != pdf_sha256:
            logger.warning(
                "Caption SHA256 (%s) does not match PDF (%s). "
                "Re-run caption-core-rules-pages before ingest.",
                captions_sha256[:12],
                pdf_sha256[:12],
            )

    logger.info("Extracting chunks from %s (%s)", pdf_path.name, parser_profile)
    chunks = extract_ingest_chunks(
        pdf_path,
        parser_profile=parser_profile,
        source=args.source_name,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        captions_by_page=captions_by_page if parser_profile == ParserProfile.CORE_RULES else None,
    )
    stats = summarize_ingest(chunks)
    logger.info(
        "Prepared %s chunks (%s on figure pages across %s PDF pages)",
        stats["chunks"],
        stats["figure_chunks"],
        stats["figure_pages"],
    )

    count = ingest_to_firestore(
        chunks,
        settings=settings,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    logger.info("Ingestion complete: %s documents", count)

    if args.dry_run:
        print(
            f"\nDry-run summary: {stats['chunks']} chunks, "
            f"{stats['figure_chunks']} with figure_description, "
            f"~{stats['avg_text_chars']} avg chars/chunk embed text"
        )


if __name__ == "__main__":
    main()

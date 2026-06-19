"""Chunk 11th edition core rules PDFs by numbered rule boundaries (e.g. 01.03).

One chunk per rule number matches how players cite rules and how GW structures the
#New40k PDF. Audit-driven edge cases (Unicode hyphens, wrapped titles, FAQ tails,
glossary-vs-flowchart dedupe) are documented in docs/core_rules_chunking.md.

Re-validate after changes: python scripts/audit_core_rules_chunks.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import fitz

# Stubs under this length (index slashes, one-line headers) hurt retrieval more than
# they help; see audit notes in docs/core_rules_chunking.md.
MIN_CHUNK_CHARS = 50

# GW uses U+2011 in bracket keywords ([TWIN‑LINKED]); ASCII regex misses them without this.
_UNICODE_HYPHENS = "\u2010\u2011\u2012\u2013\u2014\u2015"

RULE_HEADER_RE = re.compile(
    r"([A-Z][A-Z0-9 \-/']{2,100})\s+(\d{2}\.\d{2})\b",
    re.MULTILINE,
)
KEYWORD_HEADER_RE = re.compile(
    r"\[([A-Z0-9 \-/']+)\]\s+(\d{2}\.\d{2})\b",
    re.MULTILINE,
)
_WRAP_CONTINUATION_RE = re.compile(
    r"^([A-Z][A-Z0-9 \-/']+\s+AND)\s*\n\s*([A-Z][A-Z0-9 \-/']{2,100}\s+\d{2}\.\d{2}\b)",
    re.MULTILINE,
)
# e.g. "MOVING MONSTERS\nAND VEHICLES 17.01" or "6. UNIT COMPOSITION\nAND OTHER RULES 02.06"
_WRAP_AND_PREFIX_RE = re.compile(
    r"^(?:\d+\.\s*)?([A-Z][A-Z0-9 \-/']+)\s*\n\s*(AND\s+[A-Z][A-Z0-9 \-/']+\s+\d{2}\.\d{2}\b)",
    re.MULTILINE,
)
# FAQ Q&A after ch.24 glossary is not numbered core rules; ingest via updates_and_faq/.
_FAQ_BOUNDARY_RE = re.compile(
    r"^(?:Q:\s|CONTINUED IN THE APP\b)",
    re.MULTILINE,
)
# BOYZ example datasheet embedded mid-page on the ch.02 KEYWORDS spread (audit P3).
_DATASHEET_EXAMPLE_START = re.compile(r"\n\d{2}\n\d{1,3}\s*\n(?:\s*\n)?(?=[A-Z])")
_DATASHEET_SIDEBAR_RESUME = re.compile(r"►Mixed Keywords in Units")
# PDF layout sidebars and attack walkthrough diagrams (audit P4).
_SEE_ALSO_BLOCK = re.compile(r"\nSEE ALSO\s*\n.*", re.DOTALL | re.IGNORECASE)
_FRAME_REF_LINE = re.compile(r"^\s*▪\s*Frame \d+\.\d+\s*$", re.MULTILINE)
_ATTACK_WALKTHROUGH_START = re.compile(
    r"\n(?:ATTACK SEQUENCE EXAMPLES|1\. SELECT WEAPONS\s*\nThe RED).*",
    re.DOTALL,
)
_CHAPTER_INTRO_BLEED = re.compile(
    r"\nThis section (?:supplements|explains|contains|describes|presents|details).*$",
    re.DOTALL | re.IGNORECASE,
)
_BANNER_CHAPTER_BLEED = re.compile(
    r"\n\+\+[^+\n]+\+\+\s*\nDuring the battle, your units will shoot.*$",
    re.DOTALL,
)
_DECORATIVE_CALLOUT = re.compile(
    r"\nONLY IN DEATH\s*\nDOES DUTY END\s*\n.*",
    re.DOTALL,
)
_FLOWCHART_TAIL_LINE = re.compile(
    r"^(?:[A-Z]{1,3}|X\d+|Charge Roll|Save Rolls|ALLOCATION GROUPS|"
    r"FIGHT PHASE|SHOOTING PHASE|MAKING ATTACKS|DECLARE CHARGE|"
    r"ATTACKING ATTACHED UNITS|IDENTICAL ATTACKS|Charge Move|MOVING UNITS|"
    r"PLACING UNITS IN|STRATEGIC RESERVES|FLYING AND SURGING|ADVANCED)$"
)


@dataclass(frozen=True)
class CoreRuleChunk:
    text: str
    page: int
    chunk_index: int
    rule_number: str
    title: str
    chunk_type: str = "core_rule"


def _page_at_offset(offset: int, page_starts: list[tuple[int, int]]) -> int:
    page = page_starts[0][1]
    for start, page_number in page_starts:
        if start <= offset:
            page = page_number
        else:
            break
    return page


def _normalize_pdf_text(text: str) -> str:
    """Normalize PDF typography so header regexes match GW bracket keywords."""
    for hyphen in _UNICODE_HYPHENS:
        text = text.replace(hyphen, "-")
    return text.replace("\x07", "")


def _join_wrapped_titles(text: str) -> str:
    """Merge PDF line wraps like 'ACTIVE PLAYER AND\\nOPPOSING PLAYER 01.03'."""
    previous = None
    while previous != text:
        previous = text
        text = _WRAP_CONTINUATION_RE.sub(r"\1 \2", text)
        text = _WRAP_AND_PREFIX_RE.sub(r"\1 \2", text)
    return text


def _truncate_at_faq(body: str) -> str:
    """Drop FAQ appendix content that follows the last glossary entry."""
    match = _FAQ_BOUNDARY_RE.search(body)
    if match:
        return body[: match.start()].strip()
    return body


def _strip_datasheet_example_bleed(body: str) -> str:
    """Remove the embedded BOYZ datasheet from the ch.02 KEYWORDS spread.

    Audit P3: PyMuPDF reads the example datasheet between the rule intro and the
    sidebar continuation as part of 02.05. Keep intro + sidebar; drop the example.
    """
    bleed = _DATASHEET_EXAMPLE_START.search(body)
    if bleed is None:
        return body
    resume = _DATASHEET_SIDEBAR_RESUME.search(body, bleed.end())
    if resume is None or resume.start() <= bleed.start():
        return body
    return f"{body[: bleed.start()].rstrip()}\n\n{body[resume.start() :].lstrip()}"


def _strip_ch02_trailing_layout(body: str, rule_number: str) -> str:
    """Drop ch.02 diagram number runs and ch.03 intro bleed (audit P3, 02.07)."""
    if not rule_number.startswith("02."):
        return body
    match = re.search(r"\n(?:\d+\s*\n){3,}", body)
    if match:
        return body[: match.start()].rstrip()
    return body


def _strip_trailing_flowchart_labels(text: str) -> str:
    """Remove diagram node labels left at the chunk tail."""
    lines = text.splitlines()
    while lines:
        stripped = lines[-1].strip()
        if not stripped:
            lines.pop()
            continue
        if _FLOWCHART_TAIL_LINE.match(stripped) or re.fullmatch(r"\d+\.", stripped):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def _strip_flowchart_sidebar_noise(body: str) -> str:
    """Remove SEE ALSO index sidebars, walkthrough diagrams, and chapter intro bleed."""
    text = _FRAME_REF_LINE.sub("", body)
    for pattern in (
        _ATTACK_WALKTHROUGH_START,
        _DECORATIVE_CALLOUT,
        _SEE_ALSO_BLOCK,
        _BANNER_CHAPTER_BLEED,
        _CHAPTER_INTRO_BLEED,
    ):
        text = pattern.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = text.replace("►Mixed Keywords in Units", "Mixed Keywords in Units")
    return _strip_trailing_flowchart_labels(text)


_TABLE_LABEL = re.compile(r"^[A-Z]{1,3}$")


def _strip_diagram_number_run(lines: list[str]) -> list[str]:
    """Remove runs of single-digit diagram references (e.g. ch.02 moving diagram)."""
    result: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and re.fullmatch(r"\d{1,2}", stripped):
            run_start = index
            while index < len(lines) and (
                not lines[index].strip()
                or re.fullmatch(r"\d{1,2}", lines[index].strip())
            ):
                index += 1
            run_lines = lines[run_start:index]
            digit_lines = [
                line for line in run_lines if line.strip() and re.fullmatch(r"\d{1,2}", line.strip())
            ]
            if len(digit_lines) >= 3:
                continue
            result.extend(run_lines)
        else:
            result.append(lines[index])
            index += 1
    return result


def _should_strip_margin_number(
    stripped: str,
    *,
    rule_number: str,
    page: int,
    prev: str,
    next_line: str,
) -> bool:
    if not re.fullmatch(r"\d{1,3}", stripped):
        return False

    value = int(stripped)
    chapter = rule_number.split(".")[0]

    # Example walkthrough page footers between weapon abbreviations and numbered steps.
    if _TABLE_LABEL.match(prev) and value >= 19 and (
        not next_line or re.match(r"\d+\.\s", next_line)
    ):
        return True

    # Flowchart node label beside a page footer (e.g. C / 70).
    if len(prev) == 1 and prev.isalpha() and value >= 8:
        return True

    # Next-chapter margin before a section title (e.g. 17 / MONSTERS AND VEHICLES).
    if value > int(chapter) and value <= 24 and next_line.isupper() and len(next_line) > 3:
        return True

    # Phase transition margin (e.g. SHOOTING PHASE / 10).
    if prev.endswith("PHASE") and re.fullmatch(r"\d{2}", stripped) and stripped != chapter:
        return True

    # Page footer after a ++ banner line.
    if prev.startswith("++") and value >= 8:
        return True

    # Page footer on a mostly empty line before a section heading.
    if (
        not prev
        and value >= page
        and value <= page + 5
        and next_line
        and next_line.strip().isupper()
    ):
        return True

    # Page footer after a short all-caps section label (e.g. ADVANCED / 74).
    if value == page and prev.isupper() and len(prev) <= 15:
        return True

    # Page footer in a multi-page section span (e.g. ADVANCED / 76 two pages later).
    if prev.isupper() and len(prev) >= 8 and page <= value <= page + 5:
        return True

    # Page footer after a short title-case label (e.g. Terrain Area / 47).
    if (
        value in {page, page + 1, page + 2}
        and prev
        and prev[0].isupper()
        and not prev.isupper()
        and len(prev.split()) <= 3
    ):
        return True

    # Stray next-chapter marker before body text (e.g. 06 / This section contains...).
    if re.fullmatch(r"\d{2}", stripped) and stripped != chapter and next_line.startswith("This "):
        return True

    # Page footer after an all-caps section heading in multi-page walkthrough chunks.
    if (
        prev.isupper()
        and len(prev) > 8
        and re.fullmatch(r"\d{2}", stripped)
        and page <= value <= page + 5
    ):
        return True

    # Page footer after a rules sub-heading (e.g. Save Rolls / 23).
    if prev.endswith("Rolls") and value >= 8:
        return True

    # Page footer after a prose line before a section heading (e.g. visibility note / 26).
    if (
        prev.endswith(".")
        and next_line.isupper()
        and len(next_line) > 5
        and value >= page
        and value <= page + 5
    ):
        return True

    if _TABLE_LABEL.match(next_line):
        return False

    if stripped == chapter:
        return True
    if prev == chapter and value >= 8:
        return True

    if value >= 8 and value in {page - 1, page, page + 1}:
        if not next_line:
            return True
        if prev.endswith((".", ":", '"', "++", ")", "]")) or prev.startswith("++"):
            return True
        if next_line.startswith("++") or (next_line.isupper() and len(next_line) > 3):
            return True

    return False


def _strip_inline_pdf_artifacts(text: str, *, rule_number: str, page: int) -> str:
    """Remove page footers, chapter margin markers, and diagram number runs (audit P2)."""
    previous = None
    while previous != text:
        previous = text
        lines = _strip_diagram_number_run(text.splitlines())
        cleaned: list[str] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                cleaned.append(line)
                continue
            prev = next(
                (cleaned[j].strip() for j in range(len(cleaned) - 1, -1, -1) if cleaned[j].strip()),
                "",
            )
            next_line = next(
                (lines[i].strip() for i in range(index + 1, len(lines)) if lines[i].strip()),
                "",
            )
            if _should_strip_margin_number(
                stripped,
                rule_number=rule_number,
                page=page,
                prev=prev,
                next_line=next_line,
            ):
                continue
            cleaned.append(line)
        text = _strip_trailing_page_footers("\n".join(cleaned))
    return text


def _strip_trailing_page_footers(text: str) -> str:
    """Remove PDF page-number footers (audit P2).

    GW footers are bare 1-3 digit lines at chunk end (e.g. ``\\n8``). Phase step
    markers like ``2.`` are kept - they include a trailing period.
    """
    previous = None
    while previous != text:
        previous = text
        text = text.rstrip()
        lines = text.splitlines()
        if not lines:
            break
        if re.fullmatch(r"\d{1,3}", lines[-1].strip()):
            text = "\n".join(lines[:-1]).rstrip()
    return text


def _clean_chunk_text(text: str, *, rule_number: str, page: int) -> str:
    return _strip_inline_pdf_artifacts(text, rule_number=rule_number, page=page)


def _collect_header_matches(full_text: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    matches.extend(RULE_HEADER_RE.finditer(full_text))
    matches.extend(KEYWORD_HEADER_RE.finditer(full_text))
    matches.sort(key=lambda match: match.start())

    deduped: list[re.Match[str]] = []
    last_start = -1
    for match in matches:
        if match.start() == last_start:
            continue
        if deduped and match.start() < deduped[-1].end():
            continue
        deduped.append(match)
        last_start = match.start()
    return deduped


def _title_from_match(match: re.Match[str]) -> tuple[str, str]:
    if match.re is KEYWORD_HEADER_RE:
        return match.group(1).strip(), "keyword"
    return match.group(1).strip(), "core_rule"


def _prefer_chunk(candidate: CoreRuleChunk, incumbent: CoreRuleChunk) -> bool:
    """Prefer glossary entries over earlier inline cross-references.

    Audit: the same rule number often appears on a flowchart page and again in the
    ch.24 glossary (e.g. 24.11 EXTRA ATTACKS). Later page wins; longer body breaks ties.
    """
    if candidate.page != incumbent.page:
        return candidate.page > incumbent.page
    return len(candidate.text) > len(incumbent.text)


def _dedupe_by_rule_number(chunks: list[CoreRuleChunk]) -> list[CoreRuleChunk]:
    best: dict[str, CoreRuleChunk] = {}
    for chunk in chunks:
        existing = best.get(chunk.rule_number)
        if existing is None or _prefer_chunk(chunk, existing):
            best[chunk.rule_number] = chunk
    ordered = sorted(best.values(), key=lambda chunk: chunk.rule_number)
    return [
        CoreRuleChunk(
            text=chunk.text,
            page=chunk.page,
            chunk_index=index,
            rule_number=chunk.rule_number,
            title=chunk.title,
            chunk_type=chunk.chunk_type,
        )
        for index, chunk in enumerate(ordered)
    ]


def _join_pages(doc: fitz.Document) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    page_starts: list[tuple[int, int]] = []
    offset = 0
    for page_number, page in enumerate(doc, start=1):
        page_starts.append((offset, page_number))
        text = page.get_text("text")
        parts.append(text)
        offset += len(text) + 1
    return "\n".join(parts), page_starts


def parse_core_rules_text(
    full_text: str,
    *,
    page_starts: list[tuple[int, int]],
    min_chunk_chars: int = MIN_CHUNK_CHARS,
) -> list[CoreRuleChunk]:
    """Split document text on TITLE + NN.NN and [KEYWORD] + NN.NN rule headers."""
    normalized = _join_wrapped_titles(_normalize_pdf_text(full_text))
    matches = _collect_header_matches(normalized)
    if not matches:
        return []

    raw_chunks: list[CoreRuleChunk] = []
    for match in matches:
        title, chunk_type = _title_from_match(match)
        rule_number = match.group(2)
        body_start = match.end()
        next_match = next((m for m in matches if m.start() > match.start()), None)
        body_end = next_match.start() if next_match else len(normalized)
        rule_body = _truncate_at_faq(normalized[body_start:body_end].strip())
        rule_body = _strip_datasheet_example_bleed(rule_body)
        rule_body = _strip_ch02_trailing_layout(rule_body, rule_number)
        rule_body = _strip_flowchart_sidebar_noise(rule_body)
        if chunk_type == "keyword":
            header = f"[{title}] {rule_number}"
        else:
            header = f"{title} {rule_number}"
        full_chunk = f"{header}\n{rule_body}" if rule_body else header
        chunk_page = _page_at_offset(match.start(), page_starts)
        full_chunk = _clean_chunk_text(full_chunk, rule_number=rule_number, page=chunk_page)
        raw_chunks.append(
            CoreRuleChunk(
                text=full_chunk,
                page=chunk_page,
                chunk_index=0,
                rule_number=rule_number,
                title=title,
                chunk_type=chunk_type,
            )
        )

    filtered = [chunk for chunk in raw_chunks if len(chunk.text) >= min_chunk_chars]
    return _dedupe_by_rule_number(filtered)


def parse_core_rules_pdf(pdf_path: str | Path | fitz.Document) -> list[CoreRuleChunk]:
    """Extract one chunk per numbered core rule across the full PDF."""
    owns_doc = not isinstance(pdf_path, fitz.Document)
    doc = fitz.open(pdf_path) if owns_doc else pdf_path
    try:
        full_text, page_starts = _join_pages(doc)
        return parse_core_rules_text(full_text, page_starts=page_starts)
    finally:
        if owns_doc:
            doc.close()

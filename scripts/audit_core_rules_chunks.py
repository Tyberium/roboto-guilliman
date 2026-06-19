"""Thorough core-rules chunk quality audit (three passes).

Checks structural coverage, PDF contamination, and retrieval readiness. Design
context and expected baselines: docs/core_rules_chunking.md
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from roboto_guilliman.ingestion.parsers.core_rules import parse_core_rules_pdf

PDF = Path(
    "data/rules/core_rules/new40k_-_core_rules__eng_01-06_warhammer40k_new40k_core_rules-was6fbu1ix-hfewhmxyiy.pdf"
)

CONTAMINATION_PATTERNS = [
    (r"Frame \d+\.\d+", "flowchart_frame_ref"),
    (r"►", "flowchart_marker"),
    (r"^\s*/\s*$", "index_slash_stub"),
    (r"SELECT TARGETS", "shooting_flowchart"),
    (r"IDENTICAL ATTACKS", "attack_flowchart"),
    (r"^\d+\s*$", "bare_page_number"),
]

STRATAGEM_MARKERS = ("1CP", "2CP", "WHEN:", "TARGET:", "EFFECT:", "RESTRICTIONS:")
ACTION_MARKERS = ("MAXIMUM DISTANCE:", "ELIGIBLE IF:", "EFFECT:", "BEFORE MOVING:", "AFTER MOVING:")


@dataclass
class Finding:
    severity: str  # ok, warn, fail
    rule_number: str
    title: str
    page: int
    message: str


def _chapter(rule_number: str) -> str:
    return rule_number.split(".")[0]


def pass1_structural(chunks: list) -> dict:
    lengths = [len(c.text) for c in chunks]
    by_chapter: dict[str, list] = defaultdict(list)
    for c in chunks:
        by_chapter[_chapter(c.rule_number)].append(c)

    minors_by_ch: dict[str, list[int]] = {}
    gaps: dict[str, list[int]] = {}
    for ch, items in sorted(by_chapter.items(), key=lambda x: int(x[0])):
        minors = sorted(int(c.rule_number.split(".")[1]) for c in items)
        minors_by_ch[ch] = minors
        missing = [i for i in range(minors[0], minors[-1] + 1) if i not in minors]
        if missing:
            gaps[ch] = missing

    return {
        "count": len(chunks),
        "first": f"{chunks[0].rule_number} {chunks[0].title}",
        "last": f"{chunks[-1].rule_number} {chunks[-1].title}",
        "length_min": min(lengths),
        "length_avg": sum(lengths) // len(lengths),
        "length_max": max(lengths),
        "keyword_count": sum(1 for c in chunks if c.chunk_type == "keyword"),
        "core_count": sum(1 for c in chunks if c.chunk_type == "core_rule"),
        "by_chapter": {ch: len(items) for ch, items in sorted(by_chapter.items(), key=lambda x: int(x[0]))},
        "gaps": gaps,
        "short_under_120": [(c.rule_number, c.title, len(c.text)) for c in chunks if len(c.text) < 120],
        "long_over_3000": [(c.rule_number, c.title, len(c.text), c.page) for c in chunks if len(c.text) > 3000],
        "dup_titles": [(t, n) for t, n in Counter(c.title for c in chunks).items() if n > 1],
    }


def pass2_contamination(chunks: list) -> list[Finding]:
    findings: list[Finding] = []
    for c in chunks:
        for pattern, label in CONTAMINATION_PATTERNS:
            if label == "shooting_flowchart" and c.rule_number == "04.02":
                continue
            if re.search(pattern, c.text, re.MULTILINE):
                findings.append(Finding("warn", c.rule_number, c.title, c.page, label))
                break
        if c.title != c.title.strip() or c.title != c.title.upper() and c.chunk_type == "core_rule":
            if re.search(r"\d{2}\.\d{2}", c.title):
                findings.append(Finding("fail", c.rule_number, c.title, c.page, "title_contains_rule_number"))
        if c.chunk_type == "core_rule" and not c.text.startswith(f"{c.title} {c.rule_number}"):
            if not c.text.startswith(c.title):
                findings.append(Finding("warn", c.rule_number, c.title, c.page, "header_mismatch"))
    return findings


def pass3_retrieval_readiness(chunks: list) -> dict:
    stratagems = [c for c in chunks if _chapter(c.rule_number) == "15" or "STRATAGEM" in c.text.upper()]
    actions = [c for c in chunks if any(m in c.text for m in ACTION_MARKERS)]
    keywords = [c for c in chunks if c.chunk_type == "keyword"]
    battle_shock = [c for c in chunks if "BATTLE" in c.title and "SHOCK" in c.title]
    charge = [c for c in chunks if "CHARGE" in c.title or c.rule_number.startswith("11.")]

    def score_stratagem(c) -> bool:
        return sum(1 for m in STRATAGEM_MARKERS if m in c.text) >= 3

    def score_action(c) -> bool:
        return sum(1 for m in ACTION_MARKERS if m in c.text) >= 2

    return {
        "stratagems_total": len(stratagems),
        "stratagems_well_formed": sum(1 for c in stratagems if score_stratagem(c)),
        "actions_total": len(actions),
        "actions_well_formed": sum(1 for c in actions if score_action(c)),
        "keywords_total": len(keywords),
        "battle_shock_rules": [(c.rule_number, c.title, len(c.text)) for c in battle_shock],
        "charge_phase_rules": len(charge),
        "samples": {
            "battle_shock": battle_shock[0].text[:400] if battle_shock else "",
            "stratagem": next((c for c in stratagems if score_stratagem(c)), stratagems[0] if stratagems else None),
            "keyword": keywords[0] if keywords else None,
            "longest": max(chunks, key=lambda c: len(c.text)),
        },
    }


def stratified_sample(chunks: list, per_chapter: int = 1) -> list:
    by_ch: dict[str, list] = defaultdict(list)
    for c in chunks:
        by_ch[_chapter(c.rule_number)].append(c)
    sample: list = []
    for ch in sorted(by_ch, key=int):
        items = by_ch[ch]
        if len(items) <= per_chapter:
            sample.extend(items)
        else:
            step = max(1, len(items) // per_chapter)
            for i in range(0, len(items), step):
                sample.append(items[i])
                if len([x for x in sample if _chapter(x.rule_number) == ch]) >= per_chapter:
                    break
    return sorted(sample, key=lambda c: c.rule_number)


def format_chunk_preview(c, max_chars: int = 350) -> str:
    body = c.text.replace("\n", " ")
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return f"{c.rule_number} | {c.title} | p{c.page} | {c.chunk_type} | {len(c.text)} chars\n  {body}"


def main() -> None:
    chunks = parse_core_rules_pdf(PDF)
    print("=" * 72)
    print("PASS 1 — STRUCTURAL AUDIT")
    print("=" * 72)
    s1 = pass1_structural(chunks)
    for key, val in s1.items():
        if key in {"by_chapter", "gaps", "short_under_120", "long_over_3000", "dup_titles"}:
            print(f"\n{key}:")
            for item in (val.items() if isinstance(val, dict) else val):
                print(f"  {item}")
        else:
            print(f"{key}: {val}")

    print("\n" + "=" * 72)
    print("PASS 2 — CONTAMINATION & HEADER INTEGRITY")
    print("=" * 72)
    f2 = pass2_contamination(chunks)
    by_label = Counter(f.message for f in f2)
    print(f"Total flagged chunks: {len(f2)}")
    for label, count in by_label.most_common():
        print(f"  {label}: {count}")
    fails = [f for f in f2 if f.severity == "fail"]
    if fails:
        print("\nFAILURES:")
        for f in fails[:10]:
            print(f"  {f.rule_number} {f.title} p{f.page}: {f.message}")
    else:
        print("No hard failures.")

    print("\n" + "=" * 72)
    print("PASS 3 — RETRIEVAL READINESS")
    print("=" * 72)
    s3 = pass3_retrieval_readiness(chunks)
    for key, val in s3.items():
        if key == "samples":
            continue
        print(f"{key}: {val}")
    print("\nSample stratagem:", s3["samples"]["stratagem"].rule_number if s3["samples"]["stratagem"] else "n/a")
    print("\nSample keyword:", s3["samples"]["keyword"].rule_number if s3["samples"]["keyword"] else "n/a")
    longest = s3["samples"]["longest"]
    print(f"Longest chunk: {longest.rule_number} {longest.title} ({len(longest.text)} chars, p{longest.page})")

    print("\n" + "=" * 72)
    print("STRATIFIED SAMPLE — ONE PER CHAPTER (24 chapters)")
    print("=" * 72)
    sample = stratified_sample(chunks, per_chapter=1)
    for c in sample:
        print(format_chunk_preview(c))
        print()

    print("=" * 72)
    print("EXPANDED SAMPLE — STRATAGEMS + KEYWORDS + LONG CHUNKS")
    print("=" * 72)
    extras = [c for c in chunks if _chapter(c.rule_number) == "15" or c.chunk_type == "keyword" or len(c.text) > 3000]
    seen: set[str] = set()
    for c in extras:
        if c.rule_number in seen:
            continue
        seen.add(c.rule_number)
        print(format_chunk_preview(c, 450))
        print()


if __name__ == "__main__":
    main()

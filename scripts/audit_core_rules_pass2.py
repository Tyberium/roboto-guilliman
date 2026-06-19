"""Pass 2 and 3 supplemental audits."""
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from roboto_guilliman.ingestion.parsers.core_rules import parse_core_rules_pdf

PDF = Path(
    "data/rules/core_rules/new40k_-_core_rules__eng_01-06_warhammer40k_new_rules.pdf"
)
PDF = Path(
    "data/rules/core_rules/new40k_-_core_rules__eng_01-06_warhammer40k_new40k_core_rules-was6fbu1ix-hfewhmxyiy.pdf"
)
chunks = parse_core_rules_pdf(PDF)
by_num = {c.rule_number: c for c in chunks}

print("=== PASS 2B: ALL CHAPTER 15 STRATAGEMS ===")
for c in sorted([x for x in chunks if x.rule_number.startswith("15.")], key=lambda x: x.rule_number):
    markers = sum(1 for m in ("1CP", "2CP", "WHEN:", "TARGET:", "EFFECT:") if m in c.text)
    has_strat = "STRATAGEM" in c.text
    print(f"{c.rule_number} {c.title[:28]:28} p{c.page:2} len={len(c.text):4} m={markers} core_strat={has_strat}")

print("\n=== PASS 2C: CHAPTER 24 SEQUENCE ===")
ch24 = sorted(
    [c.rule_number for c in chunks if c.rule_number.startswith("24.")],
    key=lambda x: int(x.split(".")[1]),
)
print("present:", ", ".join(ch24))
all24 = set(range(1, 39))
present = {int(x.split(".")[1]) for x in ch24}
print("missing minors:", sorted(all24 - present))

print("\n=== PASS 2D: 24.37 MERGED CONTENT ===")
c37 = by_num["24.37"]
inner = list(re.finditer(r"(?:\[[A-Z0-9 \-/']+\]|FEEL NO PAIN|TWIN)\s+24\.\d{2}", c37.text))
for m in inner:
    print(" ", m.group(0)[:70])

print("\n=== PASS 2E: WRAPPED TITLE MISSES ===")
for c in chunks:
    if c.title.startswith("AND ") or "MONSTER" in c.title and c.rule_number == "17.01":
        print(f"{c.rule_number} title={c.title!r} text_start={c.text[:80]!r}")

print("\n=== PASS 3B: PAGE FOOTER NOISE ===")
footer_chunks = []
for c in chunks:
    lines = [line.strip() for line in c.text.strip().splitlines() if line.strip()]
    if lines and re.fullmatch(r"\d{1,3}", lines[-1]):
        footer_chunks.append((c.rule_number, lines[-1], c.page))
print(f"Chunks ending with bare page number: {len(footer_chunks)} / {len(chunks)}")
for item in footer_chunks[:8]:
    print(f"  {item[0]} footer={item[1]} metadata_page={item[2]}")
print("  ...")

print("\n=== PASS 3C: PLAYER QUESTION SPOT CHECKS ===")
queries = {
    "battle-shock": ["01.07", "08.03"],
    "charge roll then pick target": ["11.02", "11.03"],
    "coherency 9 inches": ["03.02"],
    "cover -1 BS": ["13.08"],
    "heroic intervention": ["15.11"],
    "lethal hits": ["24.23"],
    "opposing player": ["01.03"],
}
for topic, rules in queries.items():
    found = [r for r in rules if r in by_num]
    print(f"{topic:30} -> {found} ({'OK' if found else 'MISSING'})")
    for r in found:
        c = by_num[r]
        print(f"    {c.title} ({len(c.text)} chars, p{c.page})")

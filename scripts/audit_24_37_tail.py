"""Inspect what got swallowed into the 24.37 chunk."""
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz

from roboto_guilliman.ingestion.parsers.core_rules import parse_core_rules_pdf

PDF = Path(
    "data/rules/core_rules/new40k_-_core_rules__eng_01-06_warhammer40k_new40k_core_rules-was6fbu1ix-hfewhmxyiy.pdf"
)

doc = fitz.open(PDF)
full = "\n".join(p.get_text("text") for p in doc)
doc.close()

idx = full.find("[TORRENT] 24.37")
tail = full[idx : idx + 12000]

print("=== RAW HEADERS FROM [TORRENT] 24.37 ONWARD ===")
pat = re.compile(
    r"\[[^\]]+\]\s+24\.\d{2}|"
    r"[A-Z][A-Z0-9 \-/']{2,60}\s+24\.\d{2}|"
    r"^Q:\s",
    re.MULTILINE,
)
for m in pat.finditer(tail):
    snippet = m.group(0).replace("\n", " ")[:70]
    print(f"  @{m.start():5}: {snippet!r}")

chunks = parse_core_rules_pdf(PDF)
c37 = next(c for c in chunks if c.rule_number == "24.37")
print(f"\n24.37 chunk: {len(c37.text)} chars, page {c37.page}")
print(f"FAQ Q/A blocks: {len(re.findall(r'^Q:', c37.text, re.M))}")
print(f"Contains 'CONTINUED IN THE APP': {'CONTINUED IN THE APP' in c37.text}")
print(f"Contains TWIN-LINKED body: {'TWIN' in c37.text and '24.38' in c37.text}")

# What abilities are referenced but missing as chunks
expected = ["24.38"]
for num in expected:
    found = any(c.rule_number == num for c in chunks)
    print(f"Chunk {num} exists: {found}")

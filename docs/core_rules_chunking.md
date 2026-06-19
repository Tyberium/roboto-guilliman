# Core rules chunking (#New40k)

Design notes and audit findings for the `core_rules` parser profile. The implementation
lives in `roboto_guilliman/ingestion/parsers/core_rules.py`. Re-run quality checks with
`python scripts/audit_core_rules_chunks.py`.

**Canonical PDF:** `data/rules/core_rules/new40k_-_core_rules__eng_01-06_warhammer40k_new40k_core_rules-was6fbu1ix-hfewhmxyiy.pdf`

**Current output (post P2/P3/P4 cleanup):** 156 chunks, `01.01` through `24.38`, avg ~680 chars, **zero
contamination flags** in the three-pass audit.

---

## Why rule-number boundaries

The #New40k core rules PDF uses a stable numbered scheme (`01.03`, `15.11`, `24.23`).
Players and GW errata cite rules by number. One chunk per rule number gives:

- Predictable citations in RAG answers (`[Rule 01.07]`)
- Clean metadata (`rule_number`, `title`, `chunk_type`)
- No arbitrary token splits mid-paragraph

Other parser profiles (faction packs, missions) use different boundary logic; see
`docs/roadmap.md` section 2.

---

## Parser pipeline

```
PDF pages (PyMuPDF text)
  -> normalize Unicode hyphens
  -> merge wrapped title lines
  -> find TITLE NN.NN and [KEYWORD] NN.NN headers
  -> split body until next header
  -> truncate FAQ appendix tails (full FAQ -> updates_and_faq/ profile)
  -> strip embedded datasheet example bleed (ch.02 KEYWORDS)
  -> strip trailing page-number footers
  -> strip SEE ALSO sidebars, walkthrough diagrams, chapter intro bleed (P4)
  -> drop stubs under MIN_CHUNK_CHARS (50)
  -> dedupe by rule_number (prefer later page, then longer body)
```

### Unicode hyphen normalization

**Audit finding:** GW bracket keywords use U+2011 non-breaking hyphens, e.g.
`[TWIN‑LINKED]` and `[CLOSE‑QUARTERS]`. ASCII-only regex missed these headers.

**Effect before fix:** `[TWIN‑LINKED] 24.38` was not split; the last chunk (`24.37`
TORRENT) swallowed ~8 KB including FAQ Q&A. `[CLOSE‑QUARTERS] 24.07` was also missing.

**Fix:** Map U+2010–U+2015 to ASCII `-` before header matching.

### Title line-wrap merge

PDF text extraction breaks long titles across lines. Three patterns observed in audit:

| Pattern | Example | Result |
|---------|---------|--------|
| `FOO AND` / `BAR NN.NN` | `ACTIVE PLAYER AND` / `OPPOSING PLAYER 01.03` | Merged by `_WRAP_CONTINUATION_RE` |
| `PREFIX` / `AND SUFFIX NN.NN` | `MOVING MONSTERS` / `AND VEHICLES 17.01` | Merged by `_WRAP_AND_PREFIX_RE` |
| `N. SECTION` / `AND OTHER NN.NN` | `6. UNIT COMPOSITION` / `AND OTHER RULES 02.06` | Merged by `_WRAP_AND_PREFIX_RE` (optional `N.` prefix stripped) |

Without merge, headers like `AND VEHICLES 17.01` parse with truncated titles and hurt
embedding match quality for "monsters and vehicles movement" questions.

### Two header shapes

| Shape | Example | `chunk_type` |
|-------|---------|--------------|
| `TITLE NN.NN` | `CHARGE 11.02` | `core_rule` |
| `[KEYWORD] NN.NN` | `[LETHAL HITS] 24.23` | `keyword` |

Chapter 24 mixes both: glossary entries with brackets (`[HEAVY] 24.16`) and entries
where the PDF omits brackets (`DEADLY DEMISE 24.08`, `HOVER 24.17`). Both parse
correctly; `chunk_type` reflects header format, not rules importance.

### FAQ boundary truncation

**Audit finding:** After `[TWIN‑LINKED] 24.38`, the PDF continues with FAQ Q&A blocks
and a `CONTINUED IN THE APP` pointer (page 88). That content is not numbered core
rules and would pollute retrieval.

**Fix:** Truncate chunk bodies at the first `Q:` line or `CONTINUED IN THE APP`.
FAQ content is valuable - it will be ingested via the `updates_and_faq/` parser
profile (separate chunks, proper Q+A boundaries), not stripped from core rules.

### Trailing page-number footers (P2)

**Audit finding:** ~37 chunks contained bare 1-3 digit PDF footer lines - at chunk ends
and mid-chunk on page breaks (e.g. `\n8`, `\n43\n44`).

**Fix:** `_strip_inline_pdf_artifacts()` removes:

- Trailing bare digit lines (phase steps like `2.` kept - they have a period)
- Chapter margin markers matching the rule chapter (e.g. `\n03\n` in ch.03 rules)
- Page footers on page-break boundaries (matched against chunk page metadata)
- Diagram number runs (e.g. `1\n2\n4\n5\n6\n7\n3\n11` on the ch.02 moving spread)
- Consecutive footer pairs (strip uses already-cleaned lines as context for `prev`)

Re-run audit pass 2: `bare_page_number` should be **0**.

### Embedded datasheet example (P3)

**Audit finding:** Rule `02.05 KEYWORDS` included the full BOYZ example datasheet
(~1,400 chars of stats, weapons, wargear) because PyMuPDF reads the two-column
spread linearly. The real rule text is the intro paragraph plus the sidebar
continuation (`►Mixed Keywords in Units`).

**Fix:** When a chunk body contains the chapter/page marker + unit example pattern
followed by a sidebar resume marker, drop the middle section. Also strip ch.02 diagram
number runs and ch.03 intro bleed from `02.07` (1976 -> ~198 chars for 02.07;
02.05 1976 -> 565 chars). The sidebar resume `Mixed Keywords in Units` is kept as
plain text (arrow glyph removed).

### Flowchart and sidebar noise (P4)

**Audit finding:** ~14 chunks still contained PDF layout artifacts:

- `SEE ALSO` index sidebars (► cross-ref lists, ▪Frame 17.02)
- Attack walkthrough diagrams (`1. SELECT WEAPONS\nThe RED unit…`, ATTACK SEQUENCE EXAMPLES)
- Chapter intro bleed (`This section supplements…` after action blocks)
- Decorative callout boxes and diagram node tails (`A`, `HB`, `Charge Roll`)

**Fix:** `_strip_flowchart_sidebar_noise()` truncates at these boundaries. Rule
`04.02 SELECT TARGETS` legitimately mentions "SELECT TARGETS" in its step recap -
that is not contamination.

Re-run audit pass 2: expect **0** contamination flags.

### Minimum chunk length (50 chars)

**Audit finding:** Index stubs like `LEADER 24.22\n/` (flowchart cross-ref) and
`DATASHEET NAME 02.01` (one sentence + page footer) are valid rule numbers but too
thin for useful embeddings.

**Fix:** Drop chunks under 50 chars after split. Phase-step headers (`08.01 START OF
COMMAND PHASE`, ~109 chars) are kept; they anchor phase structure and cross-link to
neighbouring rules via `(NN.NN)` references.

### Dedupe by rule_number

**Audit finding:** The same rule number appears multiple times in the PDF:

- Inline flowchart callouts vs glossary (e.g. `[EXTRA ATTACKS] 24.11` on p17 vs p80)
- Duplicate stratagem blocks (e.g. `HEROIC INTERVENTION 15.11`)

**Policy:** Keep one chunk per rule number. Prefer the **later page** (glossary over
flowchart), then **longer body** if pages tie. Matches how players look up definitions.

---

## Audit results (2026-06, three-pass suite)

Run: `python scripts/audit_core_rules_chunks.py` (structural, contamination, retrieval).
Supplemental: `scripts/audit_core_rules_pass2.py`, `scripts/audit_24_37_tail.py`.

### Pass 1 - structural

| Metric | Value |
|--------|-------|
| Chunk count | 156 |
| Range | `01.01` ARMIES -> `24.38` TWIN-LINKED |
| Length | min 61, avg 789, max 6640 |
| Types | 134 `core_rule`, 22 `keyword` |
| Chapter 24 | 38 chunks, no sequence gaps |
| Long chunks (>3000) | `03.01`, `05.04`, `13.06` (expected - multi-step rules) |
| Short chunks (<120) | 13 phase-step headers (acceptable) |

Duplicate titles across different rule numbers are OK (`ABILITIES` at `02.03` and
`24.01`; `STRATEGIC RESERVES` at `20.01` and `20.03`).

### Pass 2 - contamination

After P2/P3/P4 fixes:

| Pattern | Count | Notes |
|---------|-------|-------|
| `bare_page_number` | **0** | Fixed (P2) |
| `flowchart_marker` (►) | **0** | Fixed (P4) |
| Flowchart frame refs | **0** | Fixed (P4) |
| `shooting_flowchart` | **0** | Fixed (P4); `04.02` title excluded from audit |

Total flagged chunks: **0** (down from 51 pre-cleanup).

### Pass 3 - retrieval readiness

| Check | Result |
|-------|--------|
| Core stratagems (ch.15) | 11/12 well-formed (`15.09` SNAP SHOOTING missing `CORE STRATAGEM` label) |
| Action blocks | 19/31 well-formed (MAX DIST / ELIGIBLE IF / EFFECT) |
| Inline `(NN.NN)` cross-refs | 42 refs, 0 broken targets |
| Player spot-checks | battle-shock, charge, coherency, cover, heroic intervention, lethal hits, opposing player - all resolve |

Preview samples: `poetry run preview-chunks --offset N --limit M`

---

## Pre-ingest quality gate

Core rules must pass P2/P3 cleanup before Firestore ingest. FAQ content is ingested
separately via `updates_and_faq/` once that parser exists.

| Priority | Issue | Status |
|----------|-------|--------|
| P2 | Trailing page-number footers | **Fixed** |
| P3 | `02.05 KEYWORDS` datasheet bleed | **Fixed** |
| P4 | Flowchart / SEE ALSO sidebar noise | **Fixed** |
| Defer | Short phase-step stubs | Thin but structurally correct; neighbours cross-link |
| Future | FAQ content | `updates_and_faq/` profile (not stripped from core rules - excluded at boundary) |

---

## What we deliberately do not chunk

| Content | Reason |
|---------|--------|
| Sep 2024 `Core Rules` PDF | 10th-ed layout; quarantined in `data/rules/excluded/` |
| FAQ appendix at end of #New40k PDF | Truncated at core-rules boundary; ingested via `updates_and_faq/` |
| Flowchart-only `24.07` node in attack sequence | Not a standalone rule; real `24.07` is `[CLOSE-QUARTERS]` glossary |
| Index `/` stubs under 50 chars | Dropped by min-length filter |

---

## Tests

Unit tests: `tests/test_core_rules_parser.py` (wrap merge, Unicode hyphens, dedupe,
FAQ truncation, keyword headers).

After parser changes, run:

```bash
python -m pytest tests/test_core_rules_parser.py -v
python scripts/audit_core_rules_chunks.py
```

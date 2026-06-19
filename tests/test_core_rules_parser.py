import re

from roboto_guilliman.ingestion.parsers.core_rules import (
    _join_wrapped_titles,
    _normalize_pdf_text,
    _strip_datasheet_example_bleed,
    _strip_flowchart_sidebar_noise,
    _strip_inline_pdf_artifacts,
    _strip_trailing_page_footers,
    parse_core_rules_text,
)


SAMPLE = """
CORE RULES
ACTIVE PLAYER AND OPPOSING PLAYER 01.03
The player whose turn it is is the Active Player.
BATTLE-SHOCK TESTS 02.01
When a unit fails a Battle-shock test, apply the effect below.
"""

WRAPPED_TITLE_SAMPLE = """
UNITS AND MODELS 01.02
Body for units and models that is long enough to survive the minimum chunk length filter comfortably.
ACTIVE PLAYER AND
OPPOSING PLAYER 01.03
The player whose turn it is is the Active Player throughout the battle.
"""

KEYWORD_SAMPLE = """
LEADER 24.22
Leader units can attach to other units following the attached unit rules in the mission pack.
[LETHAL HITS] 24.23
Each time an attack made with a Lethal Hits weapon results in a critical hit, the target is wounded automatically.
LEADER 24.22
/
"""

DUPLICATE_SAMPLE = """
HEROIC INTERVENTION 15.11
Long stratagem text here with plenty of words to pass the minimum chunk length filter easily.
HEROIC INTERVENTION 15.11
Short.
"""


def test_parse_core_rules_text_splits_on_rule_numbers() -> None:
    page_starts = [(0, 1)]
    chunks = parse_core_rules_text(SAMPLE, page_starts=page_starts)
    assert len(chunks) == 2
    assert chunks[0].rule_number == "01.03"
    assert chunks[0].title == "ACTIVE PLAYER AND OPPOSING PLAYER"
    assert "Active Player" in chunks[0].text
    assert chunks[1].rule_number == "02.01"
    assert chunks[1].title == "BATTLE-SHOCK TESTS"


def test_parse_core_rules_text_returns_empty_without_headers() -> None:
    assert parse_core_rules_text("intro only", page_starts=[(0, 1)]) == []


def test_join_wrapped_titles_merges_and_split() -> None:
    joined = _join_wrapped_titles(WRAPPED_TITLE_SAMPLE)
    assert "ACTIVE PLAYER AND OPPOSING PLAYER 01.03" in joined
    chunks = parse_core_rules_text(joined, page_starts=[(0, 1)])
    by_number = {chunk.rule_number: chunk for chunk in chunks}
    assert "ACTIVE PLAYER AND" not in by_number["01.02"].text
    assert by_number["01.03"].title == "ACTIVE PLAYER AND OPPOSING PLAYER"


def test_parse_core_rules_text_splits_keyword_headers() -> None:
    chunks = parse_core_rules_text(KEYWORD_SAMPLE, page_starts=[(0, 1)])
    by_number = {chunk.rule_number: chunk for chunk in chunks}
    assert by_number["24.22"].chunk_type == "core_rule"
    assert by_number["24.23"].chunk_type == "keyword"
    assert by_number["24.23"].title == "LETHAL HITS"
    assert "Lethal Hits weapon" in by_number["24.23"].text


def test_parse_core_rules_text_dedupes_rule_numbers() -> None:
    chunks = parse_core_rules_text(DUPLICATE_SAMPLE, page_starts=[(0, 1)])
    assert len(chunks) == 1
    assert chunks[0].rule_number == "15.11"
    assert "Long stratagem" in chunks[0].text


def test_parse_core_rules_text_drops_index_stubs() -> None:
    chunks = parse_core_rules_text(KEYWORD_SAMPLE, page_starts=[(0, 1)], min_chunk_chars=50)
    assert len(chunks) == 2
    assert "24.22" in {chunk.rule_number for chunk in chunks}


def test_parse_core_rules_text_prefers_later_page_on_duplicate_rule_numbers() -> None:
    sample = """
[EXTRA ATTACKS] 24.11
Inline reference on an earlier page with flowchart noise repeated many times to inflate length significantly.
[EXTRA ATTACKS] 24.11
The real glossary entry for extra attacks weapons on a later page with clear rules text and enough words to pass filters.
""".strip()
    second = sample.index("[EXTRA ATTACKS] 24.11\nThe real")
    page_starts = [(0, 17), (second, 81)]
    chunks = parse_core_rules_text(sample, page_starts=page_starts, min_chunk_chars=50)
    assert len(chunks) == 1
    assert chunks[0].page == 81
    assert "real glossary entry" in chunks[0].text


def test_normalize_pdf_text_replaces_unicode_hyphens() -> None:
    assert _normalize_pdf_text("[TWIN\u2011LINKED]") == "[TWIN-LINKED]"


def test_parse_core_rules_text_splits_unicode_hyphen_keyword() -> None:
    sample = """
[TORRENT] 24.37
Torrent weapons automatically hit their target when making attacks with sufficient words here.
[TWIN\u2011LINKED] 24.38
Each time an attack is made with a Twin-linked weapon you can re-roll the wound roll easily.
""".strip()
    chunks = parse_core_rules_text(sample, page_starts=[(0, 1)], min_chunk_chars=50)
    by_number = {chunk.rule_number: chunk for chunk in chunks}
    assert "24.37" in by_number
    assert "24.38" in by_number
    assert by_number["24.38"].title == "TWIN-LINKED"
    assert "Twin-linked weapon" in by_number["24.38"].text


def test_join_wrapped_titles_merges_and_prefix_line() -> None:
    sample = """
MOVING MONSTERS
AND VEHICLES 17.01
Each time you make a normal or advance move with a unit, MONSTER and VEHICLE models can move freely.
""".strip()
    joined = _join_wrapped_titles(sample)
    assert "MOVING MONSTERS AND VEHICLES 17.01" in joined
    chunks = parse_core_rules_text(joined, page_starts=[(0, 1)])
    assert chunks[0].title == "MOVING MONSTERS AND VEHICLES"


def test_join_wrapped_titles_merges_numbered_section_and_line() -> None:
    sample = """
6. UNIT COMPOSITION
AND OTHER RULES 02.06
This section details the number and types of models in the army roster for your force.
""".strip()
    joined = _join_wrapped_titles(sample)
    assert "UNIT COMPOSITION AND OTHER RULES 02.06" in joined
    chunks = parse_core_rules_text(joined, page_starts=[(0, 1)])
    assert chunks[0].title == "UNIT COMPOSITION AND OTHER RULES"


def test_parse_core_rules_text_truncates_faq_appendix() -> None:
    sample = """
[TORRENT] 24.37
Torrent weapons automatically hit their target when making attacks with sufficient words here.
[TWIN-LINKED] 24.38
Each time an attack is made with a Twin-linked weapon you can re-roll the wound roll easily.
Q: Can a unit fight after becoming unengaged?
A: No. Sometimes a unit can become engaged after the start of the Fight step.
""".strip()
    chunks = parse_core_rules_text(sample, page_starts=[(0, 1)], min_chunk_chars=50)
    by_number = {chunk.rule_number: chunk for chunk in chunks}
    assert "Q:" not in by_number["24.38"].text
    assert "CONTINUED" not in by_number["24.37"].text


def test_strip_trailing_page_footers_removes_bare_digits() -> None:
    text = "BATTLE-SHOCK 08.03\nResolve battle-shock now.\n8"
    assert _strip_trailing_page_footers(text).endswith("Resolve battle-shock now.")
    assert "8" not in _strip_trailing_page_footers(text).splitlines()[-1]


def test_strip_trailing_page_footers_keeps_phase_steps() -> None:
    text = "START OF COMMAND PHASE 08.01\nRules resolved now.\n2."
    assert _strip_trailing_page_footers(text).endswith("2.")


def test_strip_datasheet_example_bleed_keeps_sidebar_continuation() -> None:
    body = """
Datasheets have keywords in KEYWORD BOLD.
02
10

BOYZ
M
T
SV
RANGED WEAPONS
RANGE
A
SEE ALSO
►Mixed Keywords in Units
Some rules are linked to one or more keywords and this text must remain in the chunk.
""".strip()
    cleaned = _strip_datasheet_example_bleed(body)
    assert "BOYZ" not in cleaned
    assert "RANGED WEAPONS" not in cleaned
    assert "Mixed Keywords in Units" in cleaned
    assert "Some rules are linked" in cleaned


def test_strip_flowchart_sidebar_noise_removes_see_also() -> None:
    body = """
Battle-shocked units cannot control objectives.
SEE ALSO
DICE
►Modifying Dice Rolls
BATTLEFIELD MORALE
The morale and organisation of troops can waver.
""".strip()
    cleaned = _strip_flowchart_sidebar_noise(body)
    assert "SEE ALSO" not in cleaned
    assert "►" not in cleaned
    assert "Battle-shocked" in cleaned


def test_strip_flowchart_sidebar_noise_removes_attack_walkthrough() -> None:
    body = """
3. Resolve Damage: If that attack inflicts damage, the model is destroyed.
1. SELECT WEAPONS
The RED unit is attacking.
2. SELECT TARGETS
The BLUE unit is selected.
""".strip()
    cleaned = _strip_flowchart_sidebar_noise(body)
    assert "The RED" not in cleaned
    assert "Resolve Damage" in cleaned


def test_strip_flowchart_sidebar_noise_removes_chapter_intro_bleed() -> None:
    body = """
AFTER MOVING: Your unit cannot move again this phase.
This section supplements the basic rules for moving models, explaining how
some units can fly over obstacles or surge closer to the enemy.
MAKING A SURGE MOVE
++ BANNER ++
Example diagram text here.
""".strip()
    cleaned = _strip_flowchart_sidebar_noise(body)
    assert "This section supplements" not in cleaned
    assert "cannot move again this phase" in cleaned
    assert "►" not in cleaned


def test_parse_core_rules_text_has_no_flowchart_markers_in_keywords() -> None:
    sample = """
KEYWORDS 02.05
Datasheets have keywords in KEYWORD BOLD.
02
10

BOYZ
M
T
SV
►Mixed Keywords in Units
Some rules are linked to one or more keywords and this text must remain in the chunk.
UNIT COMPOSITION AND OTHER RULES 02.06
Other rule text here with enough characters to pass the minimum chunk length filter easily.
""".strip()
    chunks = parse_core_rules_text(sample, page_starts=[(0, 1)], min_chunk_chars=50)
    keywords = next(chunk for chunk in chunks if chunk.rule_number == "02.05")
    assert "►" not in keywords.text
    assert "Mixed Keywords in Units" in keywords.text



def test_strip_inline_artifacts_removes_consecutive_page_footers() -> None:
    body = """
OBJECTIVE CONSOLIDATION

43

44

BATTLEFIELDS
""".strip()
    cleaned = _strip_inline_pdf_artifacts(body, rule_number="12.09", page=42)
    assert "43" not in cleaned.splitlines()
    assert "44" not in cleaned.splitlines()
    assert "BATTLEFIELDS" in cleaned


def test_parse_core_rules_text_strips_page_footer_on_chunk() -> None:
    sample = """
ARMIES 01.01
Each player commands an army made up of units of models in Warhammer 40,000 battles.
8
""".strip()
    chunks = parse_core_rules_text(sample, page_starts=[(0, 8)], min_chunk_chars=50)
    assert len(chunks) == 1
    assert not re.fullmatch(r"\d{1,3}", chunks[0].text.strip().splitlines()[-1].strip())

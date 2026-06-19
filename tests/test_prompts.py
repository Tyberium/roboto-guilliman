"""Tests for prompt formatting."""

from roboto_guilliman.prompts import (
    RetrievedChunk,
    build_cache_key,
    build_user_prompt,
    format_context,
    is_legacy_edition_query,
    legacy_edition_refusal,
)


def test_format_context_includes_rule_number_and_figure():
    chunks = [
        RetrievedChunk(
            text="Roll one D6.",
            page=31,
            section_hint="BATTLE-SHOCK TESTS",
            source="core_rules_11th",
            rule_number="09.01",
            figure_description="Four illustrated Battle-shock examples.",
        )
    ]
    rendered = format_context(chunks)
    assert "Rule 09.01" in rendered
    assert "Diagram on this page" in rendered
    assert "Four illustrated Battle-shock examples." in rendered


def test_format_context_includes_citation():
    chunks = [
        RetrievedChunk(
            text="Roll one D6.",
            page=17,
            section_hint="BATTLE-SHOCK TESTS",
            source="core_rules_11th",
        )
    ]
    rendered = format_context(chunks)
    assert "BATTLE-SHOCK TESTS" in rendered
    assert "page 17" in rendered
    assert "Roll one D6." in rendered


def test_build_user_prompt_includes_query():
    prompt = build_user_prompt(
        "What happens on a Battle-shock test?",
        [
            RetrievedChunk(
                text="See core rules.",
                page=1,
                section_hint=None,
                source=None,
            )
        ],
    )
    assert "Battle-shock test" in prompt
    assert "See core rules." in prompt


def test_cache_key_normalizes_whitespace_and_case():
    assert build_cache_key("  Foo Bar ") == build_cache_key("foo bar")


def test_is_legacy_edition_query_detects_old_editions():
    assert is_legacy_edition_query("How did coherency work in 9th edition?")
    assert is_legacy_edition_query("10th ed blast rules")
    assert is_legacy_edition_query("previous edition rules for overwatch")


def test_is_legacy_edition_query_allows_current_edition_questions():
    assert not is_legacy_edition_query("What is a Battle-shock test in 11th edition?")
    assert not is_legacy_edition_query("Can Orks use a stratagem in the fight phase?")


def test_legacy_edition_refusal_is_in_character():
    answer = legacy_edition_refusal()
    assert "heresy" in answer.lower()
    assert "11th edition" in answer

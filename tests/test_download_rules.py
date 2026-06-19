from roboto_guilliman.ingestion.download_rules import parse_api_hits


def test_parse_api_hits_extracts_asset_links_and_dedupes() -> None:
    hits = [
        {
            "title": "Core Rules",
            "download_categories": ["core-rules-and-key-downloads"],
            "id": {"file": "core_rules.pdf"},
        },
        {
            "title": "Duplicate Core Rules",
            "download_categories": ["core-rules-and-key-downloads"],
            "id": {"file": "core_rules.pdf"},
        },
        {
            "title": "Faction Pack: Orks",
            "download_categories": ["faction-packs"],
            "id": {"file": "orks.pdf"},
        },
    ]
    entries = parse_api_hits(hits)
    assert len(entries) == 2
    assert entries[0].title == "Core Rules"
    assert entries[0].url == "https://assets.warhammer-community.com/core_rules.pdf"
    assert entries[0].category == "core-rules-and-key-downloads"
    assert entries[1].title == "Faction Pack: Orks"

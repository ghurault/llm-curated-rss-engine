from __future__ import annotations

from pathlib import Path

import pytest

from curated_feed.opml import OpmlError, parse_opml


def test_parses_feeds_in_document_order(fixtures_dir: Path) -> None:
    feeds = parse_opml(fixtures_dir / "sources.opml")

    assert [feed.xml_url for feed in feeds] == [
        "https://loose.example.com/feed.xml",
        "https://one.example.com/rss",
        "https://two.example.com/rss",
        "https://deep.example.com/atom",
        "https://beta.example.com/feed",
    ]


def test_captures_folder_including_nesting(fixtures_dir: Path) -> None:
    folders = {
        feed.xml_url: feed.folder for feed in parse_opml(fixtures_dir / "sources.opml")
    }

    # A feed outside any folder has no folder at all.
    assert folders["https://loose.example.com/feed.xml"] is None
    assert folders["https://one.example.com/rss"] == "Alpha"
    assert folders["https://two.example.com/rss"] == "Alpha"
    # Nested folders are joined into a path.
    assert folders["https://deep.example.com/atom"] == "Alpha/Nested"
    assert folders["https://beta.example.com/feed"] == "Beta"


def test_titles_fall_back_from_title_to_text(fixtures_dir: Path) -> None:
    titles = {
        feed.xml_url: feed.title for feed in parse_opml(fixtures_dir / "sources.opml")
    }

    assert titles["https://one.example.com/rss"] == "Alpha One"
    assert titles["https://two.example.com/rss"] == "Alpha Two"


def test_duplicate_feed_kept_once_at_first_occurrence(fixtures_dir: Path) -> None:
    feeds = parse_opml(fixtures_dir / "sources.opml")

    duplicated = [
        feed for feed in feeds if feed.xml_url == "https://one.example.com/rss"
    ]
    assert len(duplicated) == 1
    assert duplicated[0].folder == "Alpha"


def test_missing_file_raises_opml_error(tmp_path: Path) -> None:
    with pytest.raises(OpmlError):
        parse_opml(tmp_path / "absent.opml")


def test_malformed_file_raises_opml_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.opml"
    path.write_text("<opml><body><outline text='unclosed'>", encoding="utf-8")

    with pytest.raises(OpmlError):
        parse_opml(path)

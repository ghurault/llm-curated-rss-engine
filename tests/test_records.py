from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from curated_feed.fetch import (
    ID_LENGTH,
    canonicalize_url,
    make_id,
    records_from_feed,
    sort_records,
    truncate,
)
from curated_feed.models import Candidate
from curated_feed.opml import Feed
from curated_feed.state import append_corpus, existing_ids, read_corpus

FEED = Feed(
    title="OPML Title", xml_url="https://example.com/feed.xml", folder="Alpha/Nested"
)
NOW = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 8, 9, 14, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)
SUMMARY_MAX_CHARS = 120

# Expectations about tests/fixtures/feed.xml.
FIXTURE_ENTRIES = 5
FIXTURE_KEPT = 3


@pytest.fixture
def records(fixtures_dir: Path) -> list[Candidate]:
    parsed, _, error = records_from_feed(
        (fixtures_dir / "feed.xml").read_bytes(),
        FEED,
        fetched_at=NOW,
        cutoff=CUTOFF,
        summary_max_chars=SUMMARY_MAX_CHARS,
    )
    assert error is None
    return parsed


@pytest.fixture
def stats(fixtures_dir: Path):
    _, feed_stats, _ = records_from_feed(
        (fixtures_dir / "feed.xml").read_bytes(),
        FEED,
        fetched_at=NOW,
        cutoff=CUTOFF,
        summary_max_chars=SUMMARY_MAX_CHARS,
    )
    return feed_stats


def by_title(records: list[Candidate], title: str) -> Candidate:
    matches = [record for record in records if record.title == title]
    assert len(matches) == 1, f"expected exactly one {title!r}, got {len(matches)}"
    return matches[0]


# --------------------------------------------------------------------------- #
# URL canonicalisation and identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://example.com/a?utm_source=feedly&id=42",
            "https://example.com/a?id=42",
        ),
        ("https://example.com/a?fbclid=xyz", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://EXAMPLE.com/a", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        (" https://example.com/a ", "https://example.com/a"),
    ],
)
def test_canonicalize_url(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_canonicalize_keeps_meaningful_query_parameters() -> None:
    assert (
        canonicalize_url("https://example.com/p?page=2&id=7")
        == "https://example.com/p?page=2&id=7"
    )


def test_id_is_a_truncated_sha256_of_the_canonical_url() -> None:
    canonical = "https://example.com/a"
    expected = hashlib.sha256(canonical.encode()).hexdigest()[:16]

    assert make_id(canonical) == expected
    assert len(make_id(canonical)) == ID_LENGTH


def test_tracked_variants_of_one_article_share_an_id(records: list[Candidate]) -> None:
    assert (
        by_title(records, "Recent Item").id == by_title(records, "Tracked Duplicate").id
    )


# --------------------------------------------------------------------------- #
# Record normalisation
# --------------------------------------------------------------------------- #


def test_written_record_has_the_schema_fields_in_order(
    tmp_path: Path, records: list[Candidate]
) -> None:
    """The corpus line, not the model, is what later phases read."""
    path = tmp_path / "2026-08-09.jsonl"
    append_corpus(path, [by_title(records, "Recent Item")])

    written = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert list(written) == [
        "id",
        "url",
        "title",
        "summary",
        "source",
        "source_url",
        "folder",
        "published_at",
        "published_missing",
        "fetched_at",
    ]
    assert written["published_at"] == "2026-08-08T09:14:00Z"
    assert written["fetched_at"] == "2026-08-09T06:00:00Z"


def test_source_comes_from_the_feed_and_folder_from_the_opml(
    records: list[Candidate],
) -> None:
    record = by_title(records, "Recent Item")

    assert record.source == "Example Feed"
    assert record.source_url == "https://example.com/feed.xml"
    assert record.folder == "Alpha/Nested"
    assert record.fetched_at == NOW


def test_summary_is_plain_text_and_truncated(records: list[Candidate]) -> None:
    summary = by_title(records, "Recent Item").summary

    assert "<p>" not in summary and "<strong>" not in summary
    assert "  " not in summary
    assert summary.startswith("First sentence of the summary, with a link and markup.")
    assert len(summary) <= SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_truncate_leaves_short_text_alone() -> None:
    assert truncate("short enough", 50) == "short enough"


def test_published_at_is_parsed_as_utc(records: list[Candidate]) -> None:
    assert by_title(records, "Recent Item").published_at == PUBLISHED
    assert by_title(records, "Recent Item").published_missing is False


def test_undated_item_is_kept_and_flagged(records: list[Candidate]) -> None:
    record = by_title(records, "Undated Item")

    assert record.published_at is None
    assert record.published_missing is True


def test_items_outside_the_window_are_dropped(records: list[Candidate], stats) -> None:
    assert [record for record in records if record.title == "Old Item"] == []
    assert stats.skipped_outside_window == 1


def test_item_without_a_url_is_skipped(records: list[Candidate], stats) -> None:
    assert [record for record in records if record.title == "Item Without A Link"] == []
    assert stats.skipped_no_url == 1


def test_stats_account_for_every_entry(records: list[Candidate], stats) -> None:
    assert stats.items_seen == FIXTURE_ENTRIES
    assert stats.kept == len(records) == FIXTURE_KEPT
    assert stats.missing_date == 1


def test_unparsable_feed_reports_an_error() -> None:
    parsed, stats, error = records_from_feed(
        b"this is not a feed",
        FEED,
        fetched_at=NOW,
        cutoff=CUTOFF,
        summary_max_chars=SUMMARY_MAX_CHARS,
    )

    assert parsed == []
    assert stats.items_seen == 0
    assert error is not None


def test_zero_lookback_disables_date_filtering(fixtures_dir: Path) -> None:
    parsed, _, _ = records_from_feed(
        (fixtures_dir / "feed.xml").read_bytes(),
        FEED,
        fetched_at=NOW,
        cutoff=None,
        summary_max_chars=SUMMARY_MAX_CHARS,
    )

    assert "Old Item" in {record.title for record in parsed}


def test_records_are_sorted_newest_first_with_undated_last(
    records: list[Candidate],
) -> None:
    ordered = sort_records(records)

    assert [record.title for record in ordered] == [
        "Tracked Duplicate",
        "Recent Item",
        "Undated Item",
    ]


# --------------------------------------------------------------------------- #
# Corpus deduplication
# --------------------------------------------------------------------------- #


def test_records_survive_a_write_and_read(
    tmp_path: Path, records: list[Candidate]
) -> None:
    path = tmp_path / "2026-08-09.jsonl"
    append_corpus(path, records)

    assert read_corpus(path) == records


def test_existing_ids_scans_every_corpus_file(
    tmp_path: Path, records: list[Candidate]
) -> None:
    append_corpus(tmp_path / "2026-08-08.jsonl", records[:1])
    append_corpus(tmp_path / "2026-08-09.jsonl", records[1:])

    assert existing_ids(tmp_path) == {record.id for record in records}


def test_append_records_writes_no_file_when_there_is_nothing_to_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "2026-08-09.jsonl"
    append_corpus(path, [])

    assert not path.exists()


def test_existing_ids_tolerates_a_missing_directory_and_junk_lines(
    tmp_path: Path,
) -> None:
    assert existing_ids(tmp_path / "absent") == set()

    path = tmp_path / "2026-08-09.jsonl"
    path.write_text('{"id": "aaaa"}\n\nnot json\n{"no_id": true}\n', encoding="utf-8")
    assert existing_ids(tmp_path) == {"aaaa"}

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from curated_feed.config import Config, Paths, PublishSettings
from curated_feed.models import Candidate, PublishedEntry, Selection, SelectionResult
from curated_feed.render import (
    ATOM_NAMESPACE,
    HEADERS_FILENAME,
    ROBOTS_FILENAME,
    content_hash,
    render_atom,
    resolve_updated,
    to_entries,
    unpublished,
    window,
    write_build,
)

NOW = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
RUN_DATE = date(2026, 8, 9)
PUBLISH = PublishSettings(
    window=100, title="Test feed", author="test", site_url="https://example.invalid"
)
PREFIXED = PUBLISH.model_copy(update={"path_prefix": "a-long-random-segment"})
WINDOW_SIZE = 2


def make_entry(
    number: int, *, hours_old: int = 1, summary: str = "", rationale: str = ""
) -> PublishedEntry:
    return PublishedEntry(
        candidate_id=f"{number:016d}",
        url=f"https://example.com/{number}",
        title=f"Title {number}",
        summary=summary,
        source="A source",
        published_at=NOW - timedelta(hours=hours_old),
        rationale=rationale,
        run_date=RUN_DATE,
    )


def make_candidate(number: int, *, summary: str = "") -> Candidate:
    return Candidate(
        id=f"{number:016d}",
        url=f"https://example.com/{number}",
        title=f"Title {number}",
        summary=summary,
        source="A source",
        source_url="https://example.com/feed.xml",
        folder=None,
        published_at=NOW,
        published_missing=False,
        fetched_at=NOW,
    )


def make_selection(*numbers: int) -> SelectionResult:
    """A day that picked `numbers`, in that order, each with its own note."""
    return SelectionResult(
        run_date=RUN_DATE,
        candidate_count=len(numbers),
        selected=[
            Selection(
                candidate_id=f"{number:016d}",
                rank=rank,
                consequence=3,
                preference=0,
                score=3.0,
                rationale=f"note {number}",
            )
            for rank, number in enumerate(numbers, start=1)
        ],
    )


def parse(feed: str) -> ET.Element:
    return ET.fromstring(feed)  # noqa: S314 - our own output


def find(element: ET.Element, path: str) -> list[ET.Element]:
    """`findall` with the Atom namespace applied to every step."""
    return element.findall(
        "/".join(f"{{{ATOM_NAMESPACE}}}{step}" for step in path.split("/"))
    )


class TestPublishedLog:
    def test_a_selection_becomes_entries_in_rank_order(self):
        candidates = [make_candidate(n) for n in (1, 2)]

        entries = to_entries(make_selection(2, 1), candidates)

        assert [entry.candidate_id for entry in entries] == [
            "0000000000000002",
            "0000000000000001",
        ]
        assert entries[0].rationale == "note 2"

    def test_the_source_description_is_carried_into_the_log(self):
        """Rendering reads the log alone, so what it publishes has to be in it."""
        candidates = [
            make_candidate(1, summary="What happened, in the source's words.")
        ]

        entries = to_entries(make_selection(1), candidates)

        assert entries[0].summary == "What happened, in the source's words."

    def test_republishing_a_day_adds_nothing(self):
        already = [make_entry(1)]
        assert unpublished([make_entry(1)], already) == []

    def test_a_new_article_is_added(self):
        assert unpublished([make_entry(2)], [make_entry(1)]) == [make_entry(2)]

    def test_the_window_keeps_the_newest(self):
        entries = [make_entry(n, hours_old=n) for n in (1, 2, 3)]
        kept = window(entries, WINDOW_SIZE)

        assert [entry.candidate_id for entry in kept] == [
            "0000000000000001",
            "0000000000000002",
        ]


class TestAtom:
    def test_the_entry_id_is_the_article_url(self):
        """Change this and a reader resurfaces items that were already read."""
        feed = parse(render_atom([make_entry(1)], publish=PUBLISH, updated=NOW))

        assert find(feed, "entry/id")[0].text == "https://example.com/1"

    def test_the_entry_timestamp_is_the_publication_date(self):
        feed = parse(
            render_atom([make_entry(1, hours_old=48)], publish=PUBLISH, updated=NOW)
        )

        assert find(feed, "entry/updated")[0].text == "2026-08-07T06:00:00Z"

    def test_an_undated_article_falls_back_to_its_run_date(self):
        entry = make_entry(1).model_copy(update={"published_at": None})
        feed = parse(render_atom([entry], publish=PUBLISH, updated=NOW))

        assert find(feed, "entry/updated")[0].text == "2026-08-09T00:00:00Z"

    def test_an_entry_with_neither_text_produces_no_summary(self):
        feed = parse(render_atom([make_entry(1)], publish=PUBLISH, updated=NOW))

        assert find(feed, "entry/summary") == []

    def test_a_rationale_alone_is_published_as_the_summary(self):
        entry = make_entry(1, rationale="Reported independently by 3 outlets.")
        feed = parse(render_atom([entry], publish=PUBLISH, updated=NOW))

        assert find(feed, "entry/summary")[0].text == entry.rationale

    def test_the_source_description_is_published_as_the_summary(self):
        """What the feed loses without it is the only text worth skimming."""
        entry = make_entry(1, summary="A panel rejected the fair-use argument.")
        feed = parse(render_atom([entry], publish=PUBLISH, updated=NOW))

        assert find(feed, "entry/summary")[0].text == entry.summary

    def test_the_rationale_follows_the_description_in_one_element(self):
        """One element because it is the one every reader is certain to show."""
        entry = make_entry(
            1,
            summary="A panel rejected the fair-use argument.",
            rationale="Reported independently by 3 outlets.",
        )
        feed = parse(render_atom([entry], publish=PUBLISH, updated=NOW))

        summaries = find(feed, "entry/summary")
        assert len(summaries) == 1
        assert summaries[0].text == f"{entry.summary}\n\n{entry.rationale}"

    def test_markup_in_a_description_is_escaped(self):
        """The corpus strips HTML, but a stray angle bracket survives stripping."""
        entry = make_entry(1, summary="Held: 1 < 2 & <b>bold</b> stays text")
        feed = render_atom([entry], publish=PUBLISH, updated=NOW)

        assert "<b>" not in feed
        assert find(parse(feed), "entry/summary")[0].text == entry.summary

    def test_the_feed_says_nothing_about_the_reader(self):
        feed = render_atom([make_entry(1)], publish=PUBLISH, updated=NOW)

        assert "Test feed" in feed
        assert (
            parse(feed)
            .find(f"{{{ATOM_NAMESPACE}}}author/{{{ATOM_NAMESPACE}}}name")
            .text
            == "test"
        )

    def test_markup_in_a_title_is_escaped(self):
        entry = make_entry(1).model_copy(
            update={"title": "A <b>bold</b> & brash title"}
        )
        feed = render_atom([entry], publish=PUBLISH, updated=NOW)

        assert "<b>" not in feed
        assert find(parse(feed), "entry/title")[0].text == entry.title

    def test_an_empty_feed_is_still_valid(self):
        feed = parse(render_atom([], publish=PUBLISH, updated=NOW))

        assert find(feed, "entry") == []


class TestDeterminism:
    def test_the_same_state_renders_to_the_same_bytes(self):
        entries = [make_entry(n) for n in (1, 2, 3)]
        first = render_atom(entries, publish=PUBLISH, updated=NOW)
        second = render_atom(list(reversed(entries)), publish=PUBLISH, updated=NOW)

        assert first == second

    def test_the_content_hash_ignores_the_feed_timestamp(self):
        entries = [make_entry(1)]
        assert content_hash(entries, publish=PUBLISH) == content_hash(
            entries, publish=PUBLISH
        )

    def test_the_content_hash_follows_the_entries(self):
        assert content_hash([make_entry(1)], publish=PUBLISH) != content_hash(
            [make_entry(2)], publish=PUBLISH
        )


class TestBuild:
    def test_the_crawler_instructions_sit_at_the_build_root(self, tmp_path: Path):
        """A host looks for both at the root, not under the secret segment."""
        config = _config(tmp_path, publish=PREFIXED)
        feed = write_build([make_entry(1)], config=config, updated=NOW)

        build = tmp_path / "build"
        assert feed.parent == build / PREFIXED.path_prefix
        assert (build / ROBOTS_FILENAME).is_file()
        assert (build / HEADERS_FILENAME).is_file()

    def test_the_headers_file_asks_for_no_indexing(self, tmp_path: Path):
        """robots.txt only asks a crawler not to fetch; this covers the rest."""
        write_build([make_entry(1)], config=_config(tmp_path), updated=NOW)

        headers = (tmp_path / "build" / HEADERS_FILENAME).read_text(encoding="utf-8")
        assert headers.splitlines()[0] == "/*"
        assert "X-Robots-Tag: noindex, nofollow" in headers


class TestUpdatedTimestamp:
    def test_unchanged_content_keeps_the_previous_timestamp(self, tmp_path: Path):
        """Otherwise every run rewrites the file and a reader re-marks every item."""
        config = _config(tmp_path)
        entries = [make_entry(1)]
        meta = tmp_path / "feed.json"

        first, changed_first = resolve_updated(
            entries, config=config, meta_path=meta, now=NOW
        )
        later = NOW + timedelta(days=1)
        second, changed_second = resolve_updated(
            entries, config=config, meta_path=meta, now=later
        )

        assert changed_first and not changed_second
        assert first == second == NOW

    def test_changed_content_moves_the_timestamp(self, tmp_path: Path):
        config = _config(tmp_path)
        meta = tmp_path / "feed.json"
        resolve_updated([make_entry(1)], config=config, meta_path=meta, now=NOW)

        later = NOW + timedelta(days=1)
        updated, changed = resolve_updated(
            [make_entry(1), make_entry(2)], config=config, meta_path=meta, now=later
        )

        assert changed
        assert updated == later


def _config(tmp_path: Path, *, publish: PublishSettings = PUBLISH) -> Config:
    return Config(
        paths=Paths(
            sources=tmp_path / "s.opml",
            corpus_dir=tmp_path / "corpus",
            policy=tmp_path / "policy.md",
            state_dir=tmp_path / "state",
            build_dir=tmp_path / "build",
        ),
        publish=publish,
    )

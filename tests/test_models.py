from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from curated_feed.models import (
    VERDICT_PAYWALLED,
    Candidate,
    Graded,
    PublishedEntry,
    Response,
    Verdict,
)

CANDIDATE_JSON = {
    "id": "9f2a1c4e8b7d0a35",
    "url": "https://example.com/article",
    "title": "Plain-text title",
    "summary": "Plain text, HTML stripped, truncated",
    "source": "Feed title",
    "source_url": "https://example.com/feed.xml",
    "folder": "Alpha/Nested",
    "published_at": "2026-08-08T09:14:00Z",
    "published_missing": False,
    "fetched_at": "2026-08-09T06:00:00Z",
}

# A line of `state/published.jsonl` from before entries carried a description.
LEGACY_ENTRY_JSON = {
    "candidate_id": "9f2a1c4e8b7d0a35",
    "url": "https://example.com/article",
    "title": "Plain-text title",
    "source": "Feed title",
    "published_at": "2026-08-08T09:14:00Z",
    "rationale": "Reported independently by 3 outlets.",
    "run_date": "2026-08-09",
}

# An article both dropped and claimed as a duplicate is mentioned twice.
CLAIMED_TWICE = 2


class TestCandidate:
    def test_round_trip_preserves_the_record_verbatim(self):
        candidate = Candidate.model_validate(CANDIDATE_JSON)
        assert json.loads(candidate.model_dump_json()) == CANDIDATE_JSON

    def test_field_order_is_the_record_schema(self):
        dumped = json.loads(Candidate.model_validate(CANDIDATE_JSON).model_dump_json())
        assert list(dumped) == list(CANDIDATE_JSON)

    def test_timestamps_are_serialised_without_sub_second_precision(self):
        """`fetched_at` comes from `datetime.now`, which carries microseconds."""
        candidate = Candidate.model_validate(
            CANDIDATE_JSON
            | {"fetched_at": datetime(2026, 8, 9, 6, 0, 0, 123456, tzinfo=UTC)}
        )
        assert (
            json.loads(candidate.model_dump_json())["fetched_at"]
            == "2026-08-09T06:00:00Z"
        )

    def test_a_missing_date_survives_the_round_trip(self):
        record = CANDIDATE_JSON | {"published_at": None, "published_missing": True}
        assert json.loads(Candidate.model_validate(record).model_dump_json()) == record

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            Candidate.model_validate(CANDIDATE_JSON | {"score": 3})


class TestPublishedEntry:
    def test_a_line_written_before_descriptions_still_reads(self):
        """The log is append-only and outlives its schema.

        Every line already in it predates the field, and rendering reads the
        whole log, so a required `summary` would break the feed rather than
        improve it.
        """
        entry = PublishedEntry.model_validate(LEGACY_ENTRY_JSON)

        assert entry.summary == ""
        assert entry.rationale == LEGACY_ENTRY_JSON["rationale"]


class TestGraded:
    def test_minimal_record_takes_the_documented_defaults(self):
        graded = Graded(index=1, consequence=2)
        assert graded.topics == []
        assert graded.independent_sources == 1
        assert not graded.discretionary

    @pytest.mark.parametrize("consequence", [0, 4])
    def test_consequence_outside_the_graded_range_is_rejected(self, consequence):
        """0 is dropped as `no-substance`, so it never reaches a graded record."""
        with pytest.raises(ValidationError):
            Graded(index=1, consequence=consequence)

    def test_an_article_cannot_be_its_own_duplicate(self):
        with pytest.raises(ValidationError):
            Graded(index=4, consequence=2, duplicates=[4])

    def test_repeated_duplicates_are_rejected(self):
        with pytest.raises(ValidationError):
            Graded(index=4, consequence=2, duplicates=[7, 7])


class TestResponse:
    def test_two_records_for_one_article_are_rejected(self):
        with pytest.raises(ValidationError):
            Response(
                graded=[Graded(index=3, consequence=1), Graded(index=3, consequence=2)]
            )

    def test_accounted_indices_spans_every_mention(self):
        response = Response(
            dropped={"no-substance": [1, 2]},
            graded=[Graded(index=5, consequence=2, duplicates=[6, 7])],
        )
        assert response.accounted_indices == {1: 1, 2: 1, 5: 1, 6: 1, 7: 1}

    def test_accounted_indices_counts_an_article_claimed_twice(self):
        """Cross-set conflicts are resolved during selection, not rejected here."""
        response = Response(
            dropped={"no-substance": [6]},
            graded=[Graded(index=5, consequence=2, duplicates=[6])],
        )
        assert response.accounted_indices == {5: 1, 6: CLAIMED_TWICE}


class TestVerdict:
    def test_the_timestamp_is_written_in_the_pinned_format(self):
        """One line of `paywall.jsonl` outlives its schema, as the log does."""
        verdict = Verdict(
            candidate_id="9f2a1c4e8b7d0a35",
            url="https://example.com/article",
            verdict=VERDICT_PAYWALLED,
            checked_at=datetime(2026, 8, 9, 6, 0, 30, 123456, tzinfo=UTC),
        )
        assert json.loads(verdict.model_dump_json())["checked_at"] == (
            "2026-08-09T06:00:30Z"
        )

    def test_the_detail_is_optional(self):
        verdict = Verdict(
            candidate_id="9f2a1c4e8b7d0a35",
            url="https://example.com/article",
            verdict=VERDICT_PAYWALLED,
            checked_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
        )
        assert verdict.detail == ""

    def test_an_invented_verdict_is_rejected(self):
        with pytest.raises(ValidationError):
            Verdict(
                candidate_id="9f2a1c4e8b7d0a35",
                url="https://example.com/article",
                verdict="probably",
                checked_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
            )

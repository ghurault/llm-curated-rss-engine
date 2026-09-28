from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from curated_feed.models import MAX_CONSEQUENCE, Candidate, Response
from curated_feed.policy import Prompt
from curated_feed.score import (
    FileScorer,
    ScoreError,
    StubScorer,
    read_response,
    score_day,
    unfence,
)
from curated_feed.state import StateError

PROMPT = Prompt(documents="documents", candidates="candidates")
STUB_ITEMS = 3
EXPECTED_ATTEMPTS = 2
CANDIDATE_COUNT = 6


def make_candidate(number: int, *, published_at: datetime | None) -> Candidate:
    return Candidate(
        id=f"{number:016d}",
        url=f"https://example.com/{number}",
        title=f"Title {number}",
        summary="A summary.",
        source="A source",
        source_url="https://example.com/feed.xml",
        folder=None,
        published_at=published_at,
        published_missing=published_at is None,
        fetched_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
    )


@pytest.fixture
def candidates() -> list[Candidate]:
    """Deliberately not in publication order, so the stub has to sort."""
    base = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
    moments = [base - timedelta(hours=n) for n in (5, 1, 3, 0, 4, 2)]
    return [make_candidate(n, published_at=m) for n, m in enumerate(moments)]


class TestStubScorer:
    def test_it_grades_the_most_recent_items(self, candidates: list[Candidate]):
        response = Response.model_validate_json(
            StubScorer(items=STUB_ITEMS).generate(candidates, PROMPT)
        )
        assert [record.index for record in response.graded] == [2, 4, 6]

    def test_graded_items_clear_the_floor_unaided(self, candidates: list[Candidate]):
        """The stub exists to publish a feed, so its picks must survive selection."""
        response = Response.model_validate_json(
            StubScorer(items=STUB_ITEMS).generate(candidates, PROMPT)
        )
        assert all(
            record.consequence == MAX_CONSEQUENCE and record.durable
            for record in response.graded
        )

    def test_every_candidate_is_accounted_for_exactly_once(
        self, candidates: list[Candidate]
    ):
        response = Response.model_validate_json(
            StubScorer(items=STUB_ITEMS).generate(candidates, PROMPT)
        )
        assert response.accounted_indices == dict.fromkeys(
            range(1, CANDIDATE_COUNT + 1), 1
        )

    def test_asking_for_more_than_there_are_grades_them_all(
        self, candidates: list[Candidate]
    ):
        response = Response.model_validate_json(
            StubScorer(items=99).generate(candidates, PROMPT)
        )
        assert len(response.graded) == CANDIDATE_COUNT
        assert response.dropped == {}

    def test_an_undated_candidate_sorts_last_rather_than_crashing(self):
        candidates = [
            make_candidate(0, published_at=None),
            make_candidate(1, published_at=datetime(2026, 8, 9, tzinfo=UTC)),
        ]
        response = Response.model_validate_json(
            StubScorer(items=1).generate(candidates, PROMPT)
        )
        assert [record.index for record in response.graded] == [2]


class TestFileScorer:
    def test_it_returns_the_saved_response(self, tmp_path: Path):
        path = tmp_path / "saved.json"
        path.write_text('{"dropped": {}, "graded": []}', encoding="utf-8")

        assert FileScorer(path).generate([], PROMPT) == path.read_text(encoding="utf-8")

    def test_a_missing_file_is_reported(self, tmp_path: Path):
        with pytest.raises(ScoreError, match="saved response"):
            FileScorer(tmp_path / "absent.json").generate([], PROMPT)

    def test_it_refuses_to_retry(self, tmp_path: Path):
        """Re-reading the same file cannot produce a different answer."""
        path = tmp_path / "saved.json"
        path.write_text("{}", encoding="utf-8")

        with pytest.raises(ScoreError, match="edit"):
            FileScorer(path).generate([], PROMPT, feedback="invalid")


class FlakyScorer:
    """Returns junk until it has been asked `until` times."""

    name = "flaky"

    def __init__(self, until: int) -> None:
        self.until = until
        self.calls = 0
        self.feedback: list[str | None] = []

    def generate(self, candidates, prompt, *, feedback=None) -> str:
        self.calls += 1
        self.feedback.append(feedback)
        if self.calls < self.until:
            return '{"graded": [{"index": 0, "consequence": 9}]}'
        return '{"dropped": {}, "graded": []}'


class TestUnfence:
    """Models wrap JSON in a Markdown fence whatever the prompt says."""

    @pytest.mark.parametrize(
        "raw",
        [
            '```json\n{"graded": []}\n```',
            '```JSON\n{"graded": []}\n```',
            '```\n{"graded": []}\n```',
            'Here is the response:\n\n```json\n{"graded": []}\n```\n',
            '```json\n{"graded": []}```',
        ],
    )
    def test_a_fenced_response_yields_its_json(self, raw: str):
        assert unfence(raw) == '{"graded": []}'

    def test_unfenced_text_is_returned_unchanged(self):
        assert unfence('{"graded": []}') == '{"graded": []}'

    def test_text_that_merely_mentions_backticks_is_left_alone(self):
        assert unfence('{"reason": "uses ``` marks"}') == '{"reason": "uses ``` marks"}'


class TestReadResponse:
    """The saved artifact is raw text, so reading it back must be as tolerant."""

    def test_a_fenced_artifact_is_read_back(self, tmp_path: Path):
        path = tmp_path / "response.json"
        path.write_text('```json\n{"dropped": {}, "graded": []}\n```', encoding="utf-8")

        assert read_response(path).graded == []

    def test_a_plain_artifact_is_read_back(self, tmp_path: Path):
        path = tmp_path / "response.json"
        path.write_text('{"dropped": {}, "graded": []}', encoding="utf-8")

        assert read_response(path).graded == []

    def test_a_missing_artifact_is_reported(self, tmp_path: Path):
        with pytest.raises(StateError, match="could not read"):
            read_response(tmp_path / "absent.json")

    def test_a_malformed_artifact_names_the_problem(self, tmp_path: Path):
        path = tmp_path / "response.json"
        path.write_text(
            '{"graded": [{"index": 0, "consequence": 9}]}', encoding="utf-8"
        )

        with pytest.raises(StateError, match="consequence"):
            read_response(path)


class FencedScorer:
    """Returns valid JSON, wrapped the way a model actually wraps it."""

    name = "fenced"

    def generate(self, candidates, prompt, *, feedback=None) -> str:
        return '```json\n{"dropped": {}, "graded": []}\n```'


class TestScoreDay:
    def test_the_raw_response_is_saved_before_parsing(self, tmp_path: Path):
        path = tmp_path / "response.json"
        with pytest.raises(ScoreError):
            score_day(FlakyScorer(until=99), [], PROMPT, raw_path=path)

        assert "consequence" in path.read_text(encoding="utf-8")

    def test_a_malformed_response_is_retried_once_with_the_error(self, tmp_path: Path):
        scorer = FlakyScorer(until=2)
        response = score_day(scorer, [], PROMPT, raw_path=tmp_path / "response.json")

        assert response.graded == []
        assert scorer.calls == EXPECTED_ATTEMPTS
        assert scorer.feedback[0] is None
        assert "consequence" in str(scorer.feedback[1])

    def test_a_fenced_response_is_accepted_without_a_retry(self, tmp_path: Path):
        """The day's one retry is not spent on punctuation."""
        path = tmp_path / "response.json"
        response = score_day(FencedScorer(), [], PROMPT, raw_path=path)

        assert response.graded == []
        assert path.read_text(encoding="utf-8").startswith("```")

    def test_a_second_failure_gives_up(self, tmp_path: Path):
        scorer = FlakyScorer(until=99)
        with pytest.raises(ScoreError, match="twice"):
            score_day(scorer, [], PROMPT, raw_path=tmp_path / "response.json")

        assert scorer.calls == EXPECTED_ATTEMPTS

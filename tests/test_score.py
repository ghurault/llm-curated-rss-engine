from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from curated_feed import claude, openai_compat
from curated_feed.config import load_config
from curated_feed.models import MAX_CONSEQUENCE, Candidate, Response
from curated_feed.policy import Prompt
from curated_feed.score import (
    FileScorer,
    ScoreError,
    StubScorer,
    make_scorer,
    parse_response,
    read_response,
    score_day,
    strip_reasoning,
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


class TestStripReasoning:
    """Open-weight reasoning models often think aloud in the response itself."""

    def test_a_think_block_before_the_json_is_removed(self):
        raw = '<think>\nIndex 3 has {braces}.\n</think>\n\n{"graded": []}'

        assert strip_reasoning(raw) == '{"graded": []}'

    def test_a_closing_tag_alone_still_ends_the_reasoning(self):
        """Some chat templates open the block in the prompt, not the response."""
        assert strip_reasoning('Index 3 matters.</think>{"graded": []}') == (
            '{"graded": []}'
        )

    def test_text_without_reasoning_is_returned_unchanged(self):
        assert strip_reasoning('{"graded": []}') == '{"graded": []}'

    def test_a_fence_inside_the_reasoning_is_not_mistaken_for_the_answer(self):
        """The reasoning goes first, or the fence search would find a draft."""
        raw = (
            '<think>Draft:\n```json\n{"graded": "draft"}\n```\n</think>\n'
            '```json\n{"dropped": {}, "graded": []}\n```'
        )

        assert parse_response(raw).graded == []


class TestReadResponse:
    """The saved artifact is raw text, so reading it back must be as tolerant."""

    def test_a_fenced_artifact_is_read_back(self, tmp_path: Path):
        path = tmp_path / "response.json"
        path.write_text('```json\n{"dropped": {}, "graded": []}\n```', encoding="utf-8")

        assert read_response(path).graded == []

    def test_an_artifact_that_thinks_aloud_is_read_back(self, tmp_path: Path):
        path = tmp_path / "response.json"
        path.write_text(
            '<think>Hmm.</think>\n{"dropped": {}, "graded": []}', encoding="utf-8"
        )

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


class TestMakeScorer:
    def test_the_anthropic_scorer_reads_its_own_table(
        self, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path
    ):
        monkeypatch.setattr(claude, "build_client", lambda timeout_seconds: None)
        config = load_config(fixtures_dir / "config.toml").with_overrides(
            score={"provider": "anthropic"}
        )

        scorer = make_scorer(config, response_path=None)

        assert isinstance(scorer, claude.AnthropicScorer)
        assert (scorer.model, scorer.effort) == ("test-model", "low")

    def test_the_openai_scorer_reads_its_own_table(
        self, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path
    ):
        endpoints: list[str] = []
        monkeypatch.setattr(
            openai_compat,
            "build_client",
            lambda *, base_url, timeout_seconds: endpoints.append(base_url),
        )
        config = load_config(fixtures_dir / "config.toml").with_overrides(
            score={"provider": "openai"}
        )

        scorer = make_scorer(config, response_path=None)

        assert isinstance(scorer, openai_compat.OpenAICompatibleScorer)
        assert (scorer.model, scorer.effort) == ("an-open-model", "")
        assert endpoints == ["http://localhost:8080/v1"]

    def test_a_missing_model_names_the_table_it_belongs_in(self, fixtures_dir: Path):
        config = load_config(fixtures_dir / "config.toml").with_overrides(
            score={"provider": "anthropic", "anthropic": {"model": ""}}
        )

        with pytest.raises(ScoreError, match=r"\[score\.anthropic\] model"):
            make_scorer(config, response_path=None)

    def test_a_missing_openai_model_names_its_table(self, fixtures_dir: Path):
        config = load_config(fixtures_dir / "config.toml").with_overrides(
            score={"provider": "openai", "openai": {"model": ""}}
        )

        with pytest.raises(ScoreError, match=r"\[score\.openai\] model"):
            make_scorer(config, response_path=None)


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

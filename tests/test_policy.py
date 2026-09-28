from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from curated_feed.export import render_markdown
from curated_feed.models import Candidate
from curated_feed.policy import (
    CANDIDATES_HEADING,
    build_prompt,
    default_prompt_path,
    read_documents,
    render_candidates,
)
from curated_feed.state import StateError


def make_candidate(number: int, *, summary: str = "A summary.") -> Candidate:
    return Candidate(
        id=f"{number:016d}",
        url=f"https://example.com/{number}",
        title=f"Title {number}",
        summary=summary,
        source=f"Source {number}",
        source_url="https://example.com/feed.xml",
        folder=None,
        published_at=datetime(2026, 8, 8, 9, 14, tzinfo=UTC),
        published_missing=False,
        fetched_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
    )


@pytest.fixture
def documents(tmp_path: Path) -> tuple[Path, Path]:
    prompt = tmp_path / "prompt.md"
    policy = tmp_path / "policy.md"
    prompt.write_text("# Prompt\n\nHow to judge.\n", encoding="utf-8")
    policy.write_text("# Policy\n\nWhat matters.\n", encoding="utf-8")
    return prompt, policy


class TestCandidateList:
    def test_numbering_starts_at_one_and_is_dense(self):
        rendered = render_candidates([make_candidate(n) for n in range(3)])
        assert [line[:3] for line in rendered.splitlines()[::2]] == [
            "[1]",
            "[2]",
            "[3]",
        ]

    def test_the_prompt_carries_no_ids(self):
        assert "id:" not in render_candidates([make_candidate(1)])

    def test_the_human_export_carries_ids(self):
        assert "id:" in render_candidates([make_candidate(1)], with_ids=True)

    def test_an_empty_summary_is_marked_rather_than_left_blank(self):
        rendered = render_candidates([make_candidate(1, summary="  ")])
        assert rendered.splitlines()[1].strip() == "(no summary)"

    def test_an_undated_item_says_so(self):
        candidate = make_candidate(1).model_copy(
            update={"published_at": None, "published_missing": True}
        )
        assert "date unknown" in render_candidates([candidate])

    def test_the_export_and_the_prompt_number_articles_alike(self):
        candidates = [make_candidate(n) for n in range(4)]
        exported = render_markdown(candidates, title="day")
        prompted = render_candidates(candidates)

        assert [line.split(" — ")[0] for line in prompted.splitlines()[::2]] == [
            line.split(" — ")[0] for line in exported.splitlines()[2::2]
        ]


class TestPrompt:
    def test_documents_are_concatenated_in_order(self, documents: tuple[Path, Path]):
        text = read_documents(*documents)
        assert text.index("How to judge") < text.index("What matters")

    def test_a_missing_document_is_reported(self, tmp_path: Path):
        with pytest.raises(StateError, match="policy document"):
            read_documents(tmp_path / "absent.md")

    def test_the_prompt_separates_the_cacheable_half(
        self, documents: tuple[Path, Path]
    ):
        prompt_path, policy_path = documents
        prompt = build_prompt(
            [make_candidate(1)], prompt_path=prompt_path, policy_path=policy_path
        )

        assert "Title 1" not in prompt.documents
        assert "How to judge" not in prompt.candidates
        assert prompt.candidates.startswith(CANDIDATES_HEADING)

    def test_the_whole_prompt_holds_both_halves(self, documents: tuple[Path, Path]):
        prompt_path, policy_path = documents
        text = build_prompt(
            [make_candidate(1)], prompt_path=prompt_path, policy_path=policy_path
        ).as_text()

        assert "How to judge" in text
        assert "What matters" in text
        assert "[1] Title 1" in text

    def test_the_scoring_specification_is_not_in_the_prompt(self, repo_root: Path):
        """The floor and the item cap are withheld from the model by design."""
        spec = (repo_root / "docs" / "scoring-spec.md").read_text(encoding="utf-8")
        prompt = default_prompt_path().read_text(encoding="utf-8")

        assert "floor" in spec
        assert "floor" not in prompt.lower()


class TestThePackagedPrompt:
    """The engine's half of the documents, which no runner can name."""

    def test_it_is_found_through_the_package(self):
        """Not through the working directory: the engine runs installed."""
        assert default_prompt_path().is_file()

    def test_it_is_what_a_prompt_carries_when_none_is_named(self, tmp_path: Path):
        policy = tmp_path / "policy.md"
        policy.write_text("# Policy\n\nWhat matters.\n", encoding="utf-8")

        documents = build_prompt([make_candidate(1)], policy_path=policy).documents

        assert default_prompt_path().read_text(encoding="utf-8").strip() in documents

    def test_a_named_prompt_replaces_it(self, documents: tuple[Path, Path]):
        """The escape hatch for trying a candidate prompt against a saved day."""
        prompt_path, policy_path = documents
        assembled = build_prompt(
            [make_candidate(1)], prompt_path=prompt_path, policy_path=policy_path
        ).documents

        assert "How to judge" in assembled
        assert "Scoring Prompt" not in assembled

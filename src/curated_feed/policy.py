"""Assembling the prompt from the policy documents and the day's candidates.

No judgement of its own: this module concatenates files and numbers a list. What
the model is told lives in two documents, and the split between them is the split
between the engine and a runner.

The scoring prompt ships with the package. It is not taste but contract: its
response section is the schema `models.Response` parses, and its consequence
scale is the range `models.Graded` validates, so a prompt and a parser that
disagree fail at six in the morning with a malformed day. The two version
together or not at all.

The editorial policy is named by the runner's configuration and is loaded at
runtime, so that a change of taste is a commit against a change in output
quality rather than a code change.

`docs/scoring-spec.md` is deliberately absent. It names the score floor and the
item cap, and a model that knows them pre-filters or pads.

The assembled prompt is kept in two parts. The documents are identical on every
run and are most of the tokens, so they are the half worth caching; the candidate
list is the half that changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from pydantic import BaseModel

from .models import TIMESTAMP_FORMAT, Candidate
from .state import StateError

DOCUMENT_SEPARATOR = "\n\n---\n\n"
CANDIDATES_HEADING = "# Candidates"
PACKAGE = "curated_feed"
PROMPT_FILENAME = "prompt.md"
NO_SUMMARY = "(no summary)"
UNKNOWN_DATE = "date unknown"

EXAMPLE_CANDIDATE = Candidate(
    id="0" * 16,
    url="https://example.com/a",
    title="A title",
    summary="A summary.",
    source="A source",
    source_url="https://example.com/feed.xml",
    folder=None,
    published_at=datetime(2026, 8, 8, 9, 14, tzinfo=UTC),
    published_missing=False,
    fetched_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
)
"""A neutral candidate, so that the doctests below have something to render."""


class Prompt(BaseModel):
    """An assembled prompt, split at the boundary between stable and variable."""

    documents: str
    candidates: str

    def as_text(self) -> str:
        """The whole prompt, for a client that cannot cache a prefix."""
        return self.documents + DOCUMENT_SEPARATOR + self.candidates


def default_prompt_path() -> Path:
    """The scoring prompt that ships with the package.

    Resolved through the package rather than the working directory, because the
    engine is installed rather than checked out wherever it runs: an image, a
    virtual environment, an editable tree. A runner's configuration cannot name
    it, which is the point — see this module's docstring.

    >>> default_prompt_path().name
    'prompt.md'
    >>> default_prompt_path().is_file()
    True
    """
    return Path(str(resources.files(PACKAGE).joinpath(PROMPT_FILENAME)))


def read_documents(*paths: Path) -> str:
    """Concatenate the policy documents, in the order given."""
    texts = []
    for path in paths:
        try:
            texts.append(path.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise StateError(f"could not read policy document {path}: {exc}") from exc
    return DOCUMENT_SEPARATOR.join(texts)


def format_candidate(candidate: Candidate, number: int, *, with_id: bool) -> str:
    """One candidate as two lines: the reference line, then the summary.

    The model refers to articles by number, so the numbering is the contract.
    Ids are for a human tracing a pick back to the corpus and are left out of the
    prompt, being long opaque hashes that models mangle.

    >>> print(format_candidate(EXAMPLE_CANDIDATE, 1, with_id=False))
    [1] A title — A source — 2026-08-08T09:14:00Z
        A summary.
    """
    published = (
        candidate.published_at.strftime(TIMESTAMP_FORMAT)
        if candidate.published_at
        else UNKNOWN_DATE
    )
    reference = f"[{number}] {candidate.title} — {candidate.source} — {published}"
    if with_id:
        reference += f" — id:{candidate.id}"
    summary = candidate.summary.strip() or NO_SUMMARY
    return f"{reference}\n    {summary}"


def render_candidates(
    candidates: Sequence[Candidate], *, with_ids: bool = False
) -> str:
    """The day's candidates as a numbered list, one item per two lines.

    >>> print(render_candidates([EXAMPLE_CANDIDATE]))
    [1] A title — A source — 2026-08-08T09:14:00Z
        A summary.
    """
    return "\n".join(
        format_candidate(candidate, number, with_id=with_ids)
        for number, candidate in enumerate(candidates, start=1)
    )


def build_prompt(
    candidates: Sequence[Candidate],
    *,
    policy_path: Path,
    prompt_path: Path | None = None,
) -> Prompt:
    """Assemble the prompt for one day.

    `prompt_path` overrides the packaged scoring prompt. Nothing in the pipeline
    passes it: it exists so that a candidate prompt can be tried against a saved
    day without reinstalling, which is the only way to evaluate a prompt change
    once the corpus and the engine live in different repositories.
    """
    return Prompt(
        documents=read_documents(prompt_path or default_prompt_path(), policy_path),
        candidates=f"{CANDIDATES_HEADING}\n\n{render_candidates(candidates)}",
    )

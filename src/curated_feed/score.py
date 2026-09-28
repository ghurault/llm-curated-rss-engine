"""Turning a day of candidates into the model's judgements.

A scorer produces the raw response text and nothing else. Persisting it,
validating it and retrying live here, in one place, so that every backend gets
the same treatment and a new one is a single small class.

Two backends so far, neither needing an API client or a key:

- `StubScorer` invents a response from publication dates alone. It exists to
  prove the stages after it — selection, rendering, publishing — against a real
  feed before a token is spent.
- `FileScorer` reads a response saved from a chat session, which is how the
  evaluation workflow drives the real pipeline.

The failure policy is decided here rather than at six in the morning: save the
raw text before parsing, retry once with the validation error appended, and on a
second failure publish nothing. The previously published feed stays valid and
served, so a failed day costs one missing day.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .config import Config, ConfigError, load_config
from .models import MAX_CONSEQUENCE, Candidate, Graded, Response
from .policy import Prompt, build_prompt
from .state import (
    StateError,
    artifact_path,
    read_corpus,
    resolve_corpus,
    write_text_artifact,
)

# Models habitually wrap JSON in a Markdown fence, with or without a preamble.
# Non-greedy so a response carrying one fenced block yields that block.
CODE_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\s*\n(?P<body>.*?)\n?\s*```", re.DOTALL)

NO_SUBSTANCE = "no-substance"
MAX_ATTEMPTS = 2
RESPONSE_STAGE = "response"


class ScoreError(Exception):
    """Raised when a usable response cannot be obtained."""


class Scorer(Protocol):
    """Anything that can produce a response for a day of candidates.

    `feedback` carries the validation error from a previous attempt, and is
    `None` on the first. A backend that cannot act on it should say so rather
    than silently repeating itself.
    """

    name: str

    def generate(
        self,
        candidates: Sequence[Candidate],
        prompt: Prompt,
        *,
        feedback: str | None = None,
    ) -> str: ...


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


class StubScorer:
    """Grades the most recently published candidates, calling nothing.

    Judgement is not the point: publishing something Feedly can ingest is. The
    graded items therefore clear the floor on consequence alone, and are marked
    durable so the staleness adjustment cannot pull them back under it.
    """

    name = "stub"

    def __init__(self, *, items: int) -> None:
        self.items = items

    def generate(
        self,
        candidates: Sequence[Candidate],
        prompt: Prompt,
        *,
        feedback: str | None = None,
    ) -> str:
        ranked = sorted(
            range(len(candidates)), key=lambda index: _recency(candidates[index])
        )
        chosen = sorted(index + 1 for index in ranked[: self.items])
        rest = sorted(set(range(1, len(candidates) + 1)) - set(chosen))

        response = Response(
            dropped={NO_SUBSTANCE: rest} if rest else {},
            graded=[
                Graded(index=index, consequence=MAX_CONSEQUENCE, durable=True)
                for index in chosen
            ],
        )
        return response.model_dump_json(indent=2)


def _recency(candidate: Candidate) -> tuple[bool, float]:
    """Newest first, undated last."""
    if candidate.published_at is None:
        return (True, 0.0)
    return (False, -candidate.published_at.timestamp())


class FileScorer:
    """Reads a response saved from a chat session.

    This is what connects the manual evaluation loop to the pipeline: export a
    day, paste it into a chat, save the JSON, and the real selection, rendering
    and publishing run against it.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def generate(
        self,
        candidates: Sequence[Candidate],
        prompt: Prompt,
        *,
        feedback: str | None = None,
    ) -> str:
        if feedback is not None:
            raise ScoreError(
                f"{self.path} did not validate: {feedback}\n"
                "Re-reading the file cannot change the answer; edit it and run again."
            )
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScoreError(
                f"could not read saved response {self.path}: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #


def score_day(
    scorer: Scorer,
    candidates: Sequence[Candidate],
    prompt: Prompt,
    *,
    raw_path: Path,
) -> Response:
    """Obtain a validated response for one day.

    The raw text of each attempt is written to `raw_path` before it is parsed, so
    a response that fails validation can still be read afterwards — fence and
    all, since what is saved is what arrived. The last attempt is the one left
    on disk.
    """
    feedback: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = scorer.generate(candidates, prompt, feedback=feedback)
        write_text_artifact(raw_path, raw)
        try:
            return parse_response(raw)
        except ValidationError as exc:
            feedback = _describe(exc)
            if attempt == MAX_ATTEMPTS:
                raise ScoreError(
                    f"the {scorer.name} scorer returned an invalid response twice; "
                    f"the last is saved at {raw_path}. Final error: {feedback}"
                ) from exc
    raise AssertionError("unreachable: the loop either returns or raises")


def parse_response(raw: str) -> Response:
    """Validate raw response text, tolerating a Markdown code fence."""
    return Response.model_validate_json(unfence(raw))


def read_response(path: Path) -> Response:
    """Read a saved response artifact.

    The artifact is the raw text the model returned, fence and all, because it
    is the audit trail for a day that went wrong. Everything that reads it back
    therefore has to be as tolerant as the parse that first accepted it — which
    is why this exists rather than a bare `read_artifact(path, Response)`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"could not read {path}: {exc}") from exc
    try:
        return parse_response(text)
    except ValidationError as exc:
        raise StateError(f"{path}: does not match Response: {_describe(exc)}") from exc


def unfence(text: str) -> str:
    r"""The JSON inside a Markdown code fence, or the text unchanged.

    Asking for bare JSON does not reliably get it: wrapping it in a fence is a
    deeply trained habit, and a response saved from a chat session almost always
    arrives fenced. Spending the day's one retry on punctuation would be absurd,
    so the fence is simply removed here.

    >>> unfence('```json\n{"graded": []}\n```')
    '{"graded": []}'
    >>> unfence('{"graded": []}')
    '{"graded": []}'
    """
    match = CODE_FENCE.search(text)
    return match.group("body") if match else text


def _describe(exc: ValidationError) -> str:
    """Validation errors as a short list, for the model to correct."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()[:5]
    )


def make_scorer(config: Config, *, response_path: Path | None) -> Scorer:
    """The scorer named in configuration.

    The provider SDK is imported only when it is the one selected, so the
    offline backends — and every other command — stay free of it.
    """
    provider = config.score.provider
    if provider == "stub":
        return StubScorer(items=config.score.stub_items)
    if provider == "file":
        if response_path is None:
            raise ScoreError(
                "the file scorer needs a saved response: pass --response PATH"
            )
        return FileScorer(response_path)
    if provider == "anthropic":
        # Deferred: importing the provider SDK costs every other command a
        # noticeable startup penalty for a module they never touch.
        from .claude import AnthropicScorer  # noqa: PLC0415

        if not config.score.model:
            raise ScoreError("the anthropic scorer needs [score] model to be set")
        return AnthropicScorer(
            model=config.score.model,
            max_tokens=config.score.max_tokens,
            effort=config.score.effort,
            timeout_seconds=config.score.timeout_seconds,
        )
    raise ScoreError(
        f"unknown scorer provider: {provider!r} (known: stub, file, anthropic)"
    )


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


def format_summary(
    response: Response, *, candidates: int, path: Path, scorer: Scorer
) -> str:
    """Human-readable run report."""
    lines = [
        "",
        f"Candidates      : {candidates}",
        f"Graded          : {len(response.graded)}",
        f"Dropped         : {sum(len(v) for v in response.dropped.values())}",
    ]
    for reason, indices in sorted(response.dropped.items()):
        lines.append(f"  {reason}: {len(indices)}")

    usage = getattr(scorer, "usage", None)
    if usage is not None:
        lines.append(f"Tokens          : {usage.format()}")
    lines.append(f"Response        : {path}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-score",
        description="Ask a scorer to judge one day of candidates.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="collection day to score (default: the most recent one)",
    )
    parser.add_argument("--provider", help="scorer to use, overriding the config")
    parser.add_argument(
        "--response",
        type=Path,
        metavar="PATH",
        help="saved response to read, for the file scorer",
    )
    parser.add_argument(
        "--corpus-dir", type=Path, help="directory holding corpus files"
    )
    parser.add_argument(
        "--scoring-prompt",
        type=Path,
        metavar="PATH",
        help="scoring prompt to use instead of the one shipped with the package",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config).with_overrides(
            paths={"corpus_dir": args.corpus_dir},
            score={"provider": args.provider},
        )
        corpus_file, day = resolve_corpus(config.paths.corpus_dir, args.date)
        candidates = read_corpus(corpus_file)
        scorer = make_scorer(config, response_path=args.response)
    except (ConfigError, StateError, ScoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prompt = build_prompt(
        candidates,
        policy_path=config.paths.policy,
        prompt_path=args.scoring_prompt,
    )
    raw_path = artifact_path(config.paths.state_dir, RESPONSE_STAGE, day)

    print(f"Scoring {len(candidates)} candidate(s) with the {scorer.name} scorer")
    try:
        response = score_day(scorer, candidates, prompt, raw_path=raw_path)
    except (ScoreError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        format_summary(
            response, candidates=len(candidates), path=raw_path, scorer=scorer
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

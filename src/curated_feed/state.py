"""Reading and writing everything the pipeline keeps on disk.

Two kinds of file. The **corpus** is one JSONL per collection day, appended to
and never rewritten. The **artifacts** are one JSON per stage per day, under the
state directory, each the output of one stage and the input of the next.

That every stage boundary is a file is what makes `--date` work: a saved
candidate set can be re-scored, or a saved response re-selected, without
refetching anything. Policy tuning is then a loop of seconds rather than a day.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .models import Candidate

CORPUS_SUFFIX = ".jsonl"
ARTIFACT_SUFFIX = ".json"


class StateError(Exception):
    """Raised when a file the pipeline depends on is missing or unreadable."""


# --------------------------------------------------------------------------- #
# Corpus
# --------------------------------------------------------------------------- #


def corpus_path(corpus_dir: Path, day: date) -> Path:
    """The corpus file for one collection day.

    >>> corpus_path(Path("/corpus"), date(2026, 8, 8)).as_posix()
    '/corpus/2026-08-08.jsonl'
    """
    return corpus_dir / f"{day.isoformat()}{CORPUS_SUFFIX}"


def corpus_day(path: Path) -> date:
    """The collection day a corpus file is named for.

    >>> corpus_day(Path("/corpus/2026-08-08.jsonl")).isoformat()
    '2026-08-08'
    """
    try:
        return date.fromisoformat(path.stem)
    except ValueError as exc:
        raise StateError(f"{path}: filename is not a date") from exc


def latest_corpus_path(corpus_dir: Path) -> Path:
    """The most recent corpus file, by date-ordered filename."""
    if not corpus_dir.is_dir():
        raise StateError(f"corpus directory not found: {corpus_dir}")
    files = sorted(corpus_dir.glob(f"*{CORPUS_SUFFIX}"))
    if not files:
        raise StateError(f"no corpus files in {corpus_dir}; run curate-fetch first")
    return files[-1]


def resolve_corpus(corpus_dir: Path, day: date | None) -> tuple[Path, date]:
    """The corpus file to work on, defaulting to the most recent collection day."""
    if day is None:
        path = latest_corpus_path(corpus_dir)
        return path, corpus_day(path)
    path = corpus_path(corpus_dir, day)
    if not path.is_file():
        raise StateError(f"no corpus file for {day.isoformat()}: {path}")
    return path, day


def read_jsonl[RecordT: BaseModel](path: Path, model: type[RecordT]) -> list[RecordT]:
    """Read and validate a JSONL file, skipping blank lines.

    A missing file reads as empty: the published log does not exist until the
    first feed is rendered, and that is not an error.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"could not read {path}: {exc}") from exc

    records: list[RecordT] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append(model.model_validate_json(stripped))
        except ValidationError as exc:
            raise StateError(f"{path}:{number}: malformed record: {exc}") from exc
    return records


def append_jsonl(path: Path, records: Iterable[BaseModel]) -> int:
    """Append records as JSONL, creating the file and its directory if needed.

    Writes nothing when there is nothing to write: an empty corpus file would
    otherwise become the "latest" file that later commands pick up.
    """
    lines = [record.model_dump_json() for record in records]
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.writelines(f"{line}\n" for line in lines)
    return len(lines)


def read_corpus(path: Path) -> list[Candidate]:
    """Read and validate a corpus file."""
    if not path.exists():
        raise StateError(f"corpus file not found: {path}")
    return read_jsonl(path, Candidate)


def append_corpus(path: Path, candidates: Iterable[Candidate]) -> int:
    """Append candidates to a corpus file."""
    return append_jsonl(path, candidates)


def existing_ids(corpus_dir: Path) -> set[str]:
    """Every id already recorded in the corpus directory.

    Reads the id field alone rather than validating whole records: this runs over
    every line of every day collected so far, and only identity matters here.
    Junk lines are skipped, because a corrupt tail must not block a fetch.
    """
    ids: set[str] = set()
    if not corpus_dir.is_dir():
        return ids
    for path in sorted(corpus_dir.glob(f"*{CORPUS_SUFFIX}")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            item_id = record.get("id") if isinstance(record, dict) else None
            if item_id:
                ids.add(item_id)
    return ids


# --------------------------------------------------------------------------- #
# Stage artifacts
# --------------------------------------------------------------------------- #


def artifact_path(state_dir: Path, stage: str, day: date) -> Path:
    """Where one stage's output for one day lives.

    >>> artifact_path(Path("/state"), "response", date(2026, 8, 8)).as_posix()
    '/state/response/2026-08-08.json'
    """
    return state_dir / stage / f"{day.isoformat()}{ARTIFACT_SUFFIX}"


def read_artifact[ArtifactT: BaseModel](
    path: Path, model: type[ArtifactT]
) -> ArtifactT:
    """Read and validate one stage artifact."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"could not read {path}: {exc}") from exc
    try:
        return model.model_validate_json(text)
    except ValidationError as exc:
        raise StateError(f"{path}: does not match {model.__name__}: {exc}") from exc


def write_artifact(path: Path, artifact: BaseModel) -> Path:
    """Write one stage artifact, indented so its daily diff stays readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def write_text_artifact(path: Path, text: str) -> Path:
    """Write a stage artifact that is not a model, such as a raw response.

    The raw text is saved before parsing, so a response that fails validation can
    still be read afterwards.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path

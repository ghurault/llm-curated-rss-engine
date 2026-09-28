"""Data contracts for the curation pipeline.

Three of these are boundaries rather than conveniences. `Candidate` is the corpus
record schema, consumed by later stages and by manual evaluation sessions.
`Response` is what comes back from the model, which is untrusted input and is
validated as such. `SelectionResult` is the record of what was chosen and why.

Validation here covers shape and range only. Conflicts *between* records — the
same article both dropped and listed as a duplicate, or a candidate the response
never mentions — are resolved during selection, where they can be recorded and
reported rather than merely rejected.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from functools import cached_property
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

# The consequence scale. Zero is a reporting threshold rather than a grade: the
# model drops those articles as `no-substance`, so no graded record carries one.
MIN_CONSEQUENCE = 1
MAX_CONSEQUENCE = 3

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Whether a candidate's page can be read. `unknown` is not a guess and never
# gates: a detector that cannot reach a page must not be able to shrink the
# feed. Defined here rather than in `paywall.py` so that `select.py` can apply
# the gate without importing the stage that makes HTTP requests.
VERDICT_FREE = "free"
VERDICT_PAYWALLED = "paywalled"
VERDICT_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Corpus
# --------------------------------------------------------------------------- #


class Candidate(BaseModel):
    """One normalised feed item.

    The field order below *is* the corpus record schema, and JSON is written in
    it. Changing the fields, their order or their meaning is a deliberate act.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    title: str
    summary: str
    source: str
    source_url: str
    folder: str | None
    published_at: datetime | None
    published_missing: bool
    fetched_at: datetime

    @field_serializer("published_at", "fetched_at")
    def _serialize_moment(self, moment: datetime | None) -> str | None:
        """Pin the on-disk timestamp format, whatever precision came in.

        `fetched_at` is a `datetime.now` value carrying microseconds, and letting
        those through would change a schema other tools already read.
        """
        if moment is None:
            return None
        return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


# --------------------------------------------------------------------------- #
# Model response
# --------------------------------------------------------------------------- #


class Graded(BaseModel):
    """The model's reading of one article, as specified in `policy/prompt.md`.

    Every field is a judgement that requires having read the article. Nothing
    here is arithmetic, and nothing here knows the floor, the item cap or the
    ordering — those belong to selection.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    consequence: int = Field(ge=MIN_CONSEQUENCE, le=MAX_CONSEQUENCE)
    topics: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    durable: bool = False
    duplicates: list[int] = Field(default_factory=list)
    independent_sources: int = Field(default=1, ge=1)
    discretionary: bool = False
    discretionary_reason: str | None = None

    @field_validator("duplicates")
    @classmethod
    def _check_duplicates_unique(cls, duplicates: list[int]) -> list[int]:
        if len(set(duplicates)) != len(duplicates):
            raise ValueError("an article is listed as a duplicate more than once")
        return duplicates

    @model_validator(mode="after")
    def _check_not_own_duplicate(self) -> Graded:
        if self.index in self.duplicates:
            raise ValueError(f"article {self.index} is listed as its own duplicate")
        return self


class Response(BaseModel):
    """A day's judgements: what was dropped, and what was graded."""

    model_config = ConfigDict(extra="forbid")

    dropped: dict[str, list[int]] = Field(default_factory=dict)
    graded: list[Graded] = Field(default_factory=list)

    @field_validator("graded")
    @classmethod
    def _check_one_record_each(cls, graded: list[Graded]) -> list[Graded]:
        indices = [record.index for record in graded]
        if len(set(indices)) != len(indices):
            raise ValueError("an article has more than one graded record")
        return graded

    @cached_property
    def accounted_indices(self) -> dict[int, int]:
        """How often the response mentions each candidate number.

        The coverage invariant wants exactly one mention per candidate. Counting
        rather than testing membership is what makes both failures visible: a
        count of zero is an article never considered, and a count above one is an
        article claimed twice.
        """
        counter: Counter[int] = Counter()
        for indices in self.dropped.values():
            counter.update(indices)
        for record in self.graded:
            counter.update([record.index, *record.duplicates])
        return dict(counter)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


class Adjustment(BaseModel):
    """One weighted term of a score, kept beside the reason it was applied."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    value: float


class Selection(BaseModel):
    """A selected article, with the arithmetic that put it there.

    `rationale` is the only field published; everything else exists for tuning
    and stays in the private selection log.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    rank: int = Field(ge=1)
    consequence: int
    preference: int
    topics: list[str] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    score: float
    discretionary: bool = False
    discretionary_reason: str | None = None
    rationale: str = ""
    duplicates: list[str] = Field(default_factory=list)


class SelectionResult(BaseModel):
    """The `state/selections/DATE.json` artifact.

    Rejects are grouped by reason code rather than listed one by one, which keeps
    the artifact small while preserving a complete account of the day.
    `anomalies` records what the response got wrong and selection had to resolve;
    an empty list is the normal case, and a growing one is a policy problem.
    """

    model_config = ConfigDict(extra="forbid")

    run_date: date
    candidate_count: int = Field(ge=0)
    selected: list[Selection] = Field(default_factory=list)
    rejected: dict[str, list[str]] = Field(default_factory=dict)
    anomalies: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Readability
# --------------------------------------------------------------------------- #


class Verdict(BaseModel):
    """One line of `state/paywall.jsonl`: whether one article can be read.

    Keyed by candidate id and written once. The log is append-only and outlives
    a run, so `detail` carries why a verdict was reached — "HTTP 403", "no
    markup" — which is the whole audit trail for a day whose feed looks short.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    url: str
    verdict: Literal["free", "paywalled", "unknown"]
    checked_at: datetime
    detail: str = ""

    @field_serializer("checked_at")
    def _serialize_moment(self, moment: datetime) -> str:
        return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


class PublishedEntry(BaseModel):
    """One line of `state/published.jsonl`, and one entry of the feed.

    Denormalised on purpose: rendering is then a function of this log alone, and
    cannot be changed by a later edit to the corpus or the selection.

    `summary` is the source's own description, carried here for the same reason
    everything else is. It defaults to empty because the log is append-only and
    outlives its schema: lines written before the field existed have to stay
    readable, and they render as they always did.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    url: str
    title: str
    summary: str = ""
    source: str
    published_at: datetime | None
    rationale: str
    run_date: date

    @field_serializer("published_at")
    def _serialize_moment(self, moment: datetime | None) -> str | None:
        if moment is None:
            return None
        return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


class FeedMeta(BaseModel):
    """What the last render produced, so the next one can tell if it changed.

    The feed-level `<updated>` moves only when the content hash does. Stamping
    the run time on every render makes a reader treat every item as modified.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str
    updated: datetime


class DeployMeta(BaseModel):
    """What the last upload sent, so an unchanged feed is not sent again.

    Written only after a real upload succeeds. A failed one leaves the previous
    hash in place, which is what makes the next run retry rather than assume.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str
    deployed_at: datetime

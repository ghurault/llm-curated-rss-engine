"""Turning the model's judgements into the day's selection.

Every number is computed here: the arithmetic, the score floor, the count cap and
the ordering. The model supplies only what needs an article read, and is never
told the floor or the cap — knowing them could only tempt it to pre-filter or
pad. Sub-scores stay on disk, so tuning a coefficient is an offline sweep over a
saved corpus rather than an API call per iteration.

This module also resolves what the response got wrong. A response can claim an
article twice, name an index that does not exist, offer two discretionary picks,
or quietly omit an article altogether. None of those should cost a day's feed, so
each is resolved by a stated rule, recorded in `anomalies`, and reported.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import ConfigError, PolicySettings, load_config
from .models import (
    MAX_CONSEQUENCE,
    VERDICT_PAYWALLED,
    Adjustment,
    Candidate,
    Graded,
    Response,
    Selection,
    SelectionResult,
    Verdict,
)
from .paywall import log_path, read_verdicts
from .score import RESPONSE_STAGE, read_response
from .state import (
    StateError,
    artifact_path,
    read_corpus,
    resolve_corpus,
    write_artifact,
)

SELECTION_STAGE = "selections"

BELOW_FLOOR = "below-floor"
OVER_CAP = "over-cap"
PAYWALLED = "paywalled"
UNACCOUNTED = "unaccounted"

RECURRENCE = "recurrence"
STALENESS = "staleness"
PENALTY_PREFIX = "penalty:"

# Only these adjustment reasons are published: each is a fixed string this
# module assigns itself. A penalty names an editorial-policy rule the model
# wrote in free text — the same risk as a topic name — so it is excluded and
# stays in the selection log only.
_ADJUSTMENT_LABELS: dict[str, str] = {
    RECURRENCE: "Reported independently by {sources} outlets",
    STALENESS: "Older than the window",
}


@dataclass(slots=True)
class Resolution:
    """The response with its conflicts settled, ready to be scored."""

    graded: list[Graded] = field(default_factory=list)
    dropped: dict[str, list[int]] = field(default_factory=dict)
    unaccounted: list[int] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Scored:
    """One graded article with its arithmetic worked out."""

    graded: Graded
    candidate: Candidate
    preference: int
    adjustments: list[Adjustment]
    score: float


# --------------------------------------------------------------------------- #
# Resolving the response
# --------------------------------------------------------------------------- #


def resolve(response: Response, count: int) -> Resolution:
    """Settle every conflicting claim, recording each resolution.

    Precedence, in order: a drop beats a grade, because hard exclusions are gates
    that nothing may bypass; a grade beats being someone else's duplicate; and
    the first group to claim a duplicate keeps it.
    """
    result = Resolution()
    valid = range(1, count + 1)

    dropped_indices = _collect_dropped(response, valid, result)
    graded_indices = _collect_graded(
        response, valid, dropped=dropped_indices, result=result
    )
    duplicate_indices = _prune_duplicates(
        valid, dropped=dropped_indices, graded=graded_indices, result=result
    )
    _limit_discretionary(result)

    accounted = dropped_indices | graded_indices | duplicate_indices
    result.unaccounted = sorted(set(valid) - accounted)
    return result


def _collect_dropped(response: Response, valid: range, result: Resolution) -> set[int]:
    """The model's own drops, which nothing later may overturn."""
    claimed: set[int] = set()
    for reason, indices in response.dropped.items():
        kept = []
        for index in indices:
            if index not in valid:
                result.anomalies.append(f"dropped index {index} does not exist")
            elif index in claimed:
                result.anomalies.append(f"article {index} was dropped twice")
            else:
                claimed.add(index)
                kept.append(index)
        if kept:
            result.dropped[reason] = sorted(kept)
    return claimed


def _collect_graded(
    response: Response, valid: range, *, dropped: set[int], result: Resolution
) -> set[int]:
    kept: set[int] = set()
    for record in response.graded:
        if record.index not in valid:
            result.anomalies.append(f"graded index {record.index} does not exist")
            continue
        if record.index in dropped:
            result.anomalies.append(
                f"article {record.index} was dropped, so its grade is ignored"
            )
            continue
        kept.add(record.index)
        result.graded.append(record)
    return kept


def _prune_duplicates(
    valid: range, *, dropped: set[int], graded: set[int], result: Resolution
) -> set[int]:
    """Keep each duplicate in exactly one group, and never one already spoken for."""
    claimed: set[int] = set()
    pruned: list[Graded] = []
    for record in result.graded:
        keep = []
        for index in record.duplicates:
            if index not in valid:
                result.anomalies.append(f"duplicate index {index} does not exist")
            elif index in dropped:
                result.anomalies.append(
                    f"article {index} was dropped, so it joins no group"
                )
            elif index in graded:
                result.anomalies.append(
                    f"article {index} is graded in its own right, "
                    f"so it is not a duplicate of {record.index}"
                )
            elif index in claimed:
                result.anomalies.append(
                    f"article {index} was claimed by an earlier group than "
                    f"{record.index}"
                )
            else:
                claimed.add(index)
                keep.append(index)
        pruned.append(record.model_copy(update={"duplicates": keep}))
    result.graded = pruned
    return claimed


def _limit_discretionary(result: Resolution) -> None:
    """At most one discretionary pick, whatever the response offered."""
    offered = [record for record in result.graded if record.discretionary]
    if len(offered) <= 1:
        return

    keeper = max(offered, key=lambda record: (record.consequence, -record.index))
    result.anomalies.append(
        f"{len(offered)} discretionary picks offered; kept article {keeper.index}"
    )
    result.graded = [
        (
            record
            if record is keeper or not record.discretionary
            else record.model_copy(update={"discretionary": False})
        )
        for record in result.graded
    ]


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #


def count_preference(topics: Sequence[str], *, cap: int) -> int:
    """One point per distinct topic, capped.

    Names are not checked against the policy: an invented topic inflates the
    score, which is a policy problem rather than a parsing one, and no model text
    is published. Case and whitespace are folded so one topic cannot count twice.

    >>> count_preference(["Privacy", "privacy", "Linux"], cap=3)
    2
    """
    distinct = {topic.strip().casefold() for topic in topics if topic.strip()}
    return min(len(distinct), cap)


def compute_adjustments(
    graded: Graded, candidate: Candidate, *, policy: PolicySettings, now: datetime
) -> list[Adjustment]:
    """The ±1-bounded terms of scoring specification §2.3, in a fixed order."""
    weights = policy.adjustments
    adjustments = []

    cap = weights.recurrence_cap_sources
    recurrence_fraction = min(graded.independent_sources - 1, cap - 1) / (cap - 1)
    if recurrence_fraction > 0:
        adjustments.append(
            Adjustment(
                reason=RECURRENCE, value=weights.recurrence * recurrence_fraction
            )
        )
    if _is_stale(graded, candidate, hours=weights.staleness_hours, now=now):
        adjustments.append(Adjustment(reason=STALENESS, value=weights.staleness))
    adjustments += [
        Adjustment(reason=f"{PENALTY_PREFIX}{rule}", value=weights.penalty)
        for rule in graded.penalties
    ]
    return adjustments


def _is_stale(
    graded: Graded, candidate: Candidate, *, hours: int, now: datetime
) -> bool:
    """Old enough to be worth less — unless age is beside the point.

    An article with no date is judged on content, and durable analysis is exempt
    because age is irrelevant to a good retrospective. Only the model can tell
    the second case, which is why it reports it.
    """
    if candidate.published_at is None or graded.durable:
        return False
    return now - candidate.published_at > timedelta(hours=hours)


def score_one(
    graded: Graded, candidate: Candidate, *, policy: PolicySettings, now: datetime
) -> Scored:
    """`score = consequence + preference + adjustments`."""
    preference = count_preference(graded.topics, cap=policy.preference_cap)
    adjustments = compute_adjustments(graded, candidate, policy=policy, now=now)
    return Scored(
        graded=graded,
        candidate=candidate,
        preference=preference,
        adjustments=adjustments,
        score=graded.consequence
        + preference
        + sum(adjustment.value for adjustment in adjustments),
    )


def build_rationale(
    graded: Graded,
    *,
    preference: int,
    adjustments: Sequence[Adjustment],
    policy: PolicySettings,
) -> str:
    """The line that is published: consequence, preference, and fixed adjustments.

    Built from numbers and from the fixed reason codes this module assigns
    itself, never from unvalidated model text: the feed is public, so the
    privacy property is structural rather than a rule someone has to
    remember. Topics and penalty rule names are both free text a model wrote
    *about the reader's own interests*, so neither reaches here; they stay in
    the selection log, which is not public.

    `discretionary_reason` is the one piece of model text that does reach
    here. `policy/prompt.md` §7 has the model write it about the article —
    what would otherwise have kept it out — never about the reader, so
    publishing it verbatim carries none of the risk that keeps topics and
    penalty rule names out.

    >>> build_rationale(
    ...     Graded(index=1, consequence=3), preference=0, adjustments=[],
    ...     policy=PolicySettings(),
    ... )
    'Consequence 3 of 3. Preference 0.'
    >>> build_rationale(
    ...     Graded(index=1, consequence=2, independent_sources=3), preference=2,
    ...     adjustments=[Adjustment(reason=RECURRENCE, value=1.0)],
    ...     policy=PolicySettings(),
    ... )
    'Consequence 2 of 3. Preference +2. Reported independently by 3 outlets (+1).'
    >>> build_rationale(
    ...     Graded(
    ...         index=1, consequence=1, discretionary=True,
    ...         discretionary_reason="an unfamiliar angle on a familiar story",
    ...     ),
    ...     preference=0, adjustments=[], policy=PolicySettings(),
    ... )
    'Consequence 1 of 3. Preference 0. Discretionary pick: an unfamiliar angle on a familiar story.'
    >>> build_rationale(
    ...     Graded(index=1, consequence=3), preference=0, adjustments=[],
    ...     policy=PolicySettings(rationale_mode="none"),
    ... )
    ''
    """
    if policy.rationale_mode == "none":
        return ""
    sentences = [
        f"Consequence {graded.consequence} of {MAX_CONSEQUENCE}.",
        f"Preference +{preference}." if preference else "Preference 0.",
    ]
    sentences += [
        _adjustment_sentence(adjustment, sources=graded.independent_sources)
        for adjustment in adjustments
        if adjustment.reason in _ADJUSTMENT_LABELS
    ]
    if graded.discretionary:
        sentences.append(_discretionary_sentence(graded.discretionary_reason))
    return " ".join(sentences)


def _adjustment_sentence(adjustment: Adjustment, *, sources: int) -> str:
    label = _ADJUSTMENT_LABELS[adjustment.reason].format(sources=sources)
    return f"{label} ({adjustment.value:+g})."


def _discretionary_sentence(reason: str | None) -> str:
    if reason:
        return f"Discretionary pick: {reason}."
    return "Discretionary pick."


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def select_day(
    response: Response,
    candidates: Sequence[Candidate],
    *,
    policy: PolicySettings,
    now: datetime,
    run_date: date,
    verdict_by_id: dict[str, Verdict] | None = None,
) -> SelectionResult:
    """Score, gate the unreadable, apply the floor, add the pick, cap, and rank.

    The order is the one in the scoring specification, and it matters: the
    discretionary pick is added after the floor so it can bypass it, and the cap
    is applied last so it is a cap on what would otherwise be published.

    The paywall gate comes first of all, and it is a gate rather than a penalty:
    an article the reader cannot open is worth nothing whatever it is about, so
    no score and no discretionary pass gets past it. Gating here rather than
    after the cap is what lets the next article take the freed slot.

    `verdict_by_id` is a parameter rather than a config lookup so that this
    module keeps reading its inputs from its arguments, and stays testable
    without a filesystem. Absent, or absent for a given candidate, means
    readable — the feature is then a no-op and a saved day replays unchanged.
    """
    resolution = resolve(response, len(candidates))
    scored = [
        score_one(record, candidates[record.index - 1], policy=policy, now=now)
        for record in resolution.graded
    ]

    gated = {item.graded.index for item in scored if _is_paywalled(item, verdict_by_id)}
    qualifying = [
        item
        for item in scored
        if item.graded.index not in gated and _qualifies(item, floor=policy.floor)
    ]
    ranked = sorted(qualifying, key=_rank_key)
    chosen = ranked[: policy.max_items]

    rejected = _collect_rejects(
        resolution,
        candidates,
        scored=scored,
        chosen=chosen,
        over_cap=ranked[policy.max_items :],
        gated=gated,
    )
    identify = [candidate.id for candidate in candidates]
    selected = [
        _to_selection(item, rank=rank, policy=policy, identify=identify)
        for rank, item in enumerate(chosen, start=1)
    ]

    _check_coverage(selected, rejected, candidates)
    return SelectionResult(
        run_date=run_date,
        candidate_count=len(candidates),
        selected=selected,
        rejected=rejected,
        anomalies=resolution.anomalies,
    )


def _is_paywalled(item: Scored, verdict_by_id: dict[str, Verdict] | None) -> bool:
    """Whether this article is known to be unreadable.

    Only `paywalled` gates. `unknown` covers a page that could not be reached
    at all, and reading that as a wall would let a detector's bad afternoon
    shorten the feed.
    """
    if not verdict_by_id:
        return False
    verdict = verdict_by_id.get(item.candidate.id)
    return verdict is not None and verdict.verdict == VERDICT_PAYWALLED


def _qualifies(item: Scored, *, floor: float) -> bool:
    """Clearing the floor, or holding the day's one discretionary pass."""
    return item.score >= floor or item.graded.discretionary


def _rank_key(item: Scored) -> tuple[float, float, str]:
    """Highest score first, then newest, then by id so runs are reproducible."""
    published = item.candidate.published_at
    return (
        -item.score,
        -published.timestamp() if published else 0.0,
        item.candidate.id,
    )


def _to_selection(
    item: Scored, *, rank: int, policy: PolicySettings, identify: Sequence[str]
) -> Selection:
    return Selection(
        candidate_id=item.candidate.id,
        rank=rank,
        consequence=item.graded.consequence,
        preference=item.preference,
        topics=list(item.graded.topics),
        adjustments=item.adjustments,
        score=item.score,
        discretionary=item.graded.discretionary,
        discretionary_reason=item.graded.discretionary_reason,
        rationale=build_rationale(
            item.graded,
            preference=item.preference,
            adjustments=item.adjustments,
            policy=policy,
        ),
        duplicates=[identify[index - 1] for index in item.graded.duplicates],
    )


def _collect_rejects(
    resolution: Resolution,
    candidates: Sequence[Candidate],
    *,
    scored: Sequence[Scored],
    chosen: Sequence[Scored],
    over_cap: Sequence[Scored],
    gated: set[int],
) -> dict[str, list[str]]:
    """Every candidate that is not published, under the reason it is not.

    Articles collapsed into a *selected* item are carried on that item instead;
    those collapsed into a rejected one are recorded here, so that a group whose
    representative did not make it is still fully accounted for.

    The reason is otherwise settled by elimination, so a gated article would be
    filed as a near miss and the day's report would name the wrong cause.
    """
    identify = [candidate.id for candidate in candidates]
    rejects: dict[str, list[str]] = {
        reason: [identify[index - 1] for index in indices]
        for reason, indices in resolution.dropped.items()
    }
    if resolution.unaccounted:
        rejects[UNACCOUNTED] = [identify[index - 1] for index in resolution.unaccounted]

    chosen_indices = {item.graded.index for item in chosen}
    over_cap_indices = {item.graded.index for item in over_cap}
    for item in scored:
        if item.graded.index in chosen_indices:
            continue
        if item.graded.index in gated:
            reason = PAYWALLED
        elif item.graded.index in over_cap_indices:
            reason = OVER_CAP
        else:
            reason = BELOW_FLOOR
        rejects.setdefault(reason, []).append(item.candidate.id)
        for index in item.graded.duplicates:
            key = f"duplicate-of:{item.candidate.id}"
            rejects.setdefault(key, []).append(identify[index - 1])

    return {reason: sorted(members) for reason, members in rejects.items()}


def _check_coverage(
    selected: Sequence[Selection],
    rejected: dict[str, list[str]],
    candidates: Sequence[Candidate],
) -> None:
    """Every candidate accounted for exactly once. A bug here, not bad input."""
    accounted = [
        *(item.candidate_id for item in selected),
        *(duplicate for item in selected for duplicate in item.duplicates),
        *(member for members in rejected.values() for member in members),
    ]
    if sorted(accounted) != sorted(candidate.id for candidate in candidates):
        raise AssertionError("selection lost or double-counted a candidate")


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


def format_summary(result: SelectionResult, *, path: Path) -> str:
    """Human-readable run report."""
    lines = [
        "",
        f"Candidates      : {result.candidate_count}",
        f"Selected        : {len(result.selected)}",
    ]
    lines += [
        f"  {item.rank}. [{item.score:g}] {item.candidate_id}"
        for item in result.selected
    ]
    lines.append("Rejected        :")
    lines += [
        f"  {reason}: {len(members)}"
        for reason, members in sorted(result.rejected.items())
    ]
    if result.anomalies:
        lines.append("Anomalies       :")
        lines += [f"  - {note}" for note in result.anomalies]
    lines.append(f"Selection       : {path}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-select",
        description="Score a saved response and assemble the day's selection.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="collection day to select from (default: the most recent one)",
    )
    parser.add_argument(
        "--corpus-dir", type=Path, help="directory holding corpus files"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config).with_overrides(
            paths={"corpus_dir": args.corpus_dir}
        )
        corpus_file, day = resolve_corpus(config.paths.corpus_dir, args.date)
        candidates = read_corpus(corpus_file)
        response = read_response(
            artifact_path(config.paths.state_dir, RESPONSE_STAGE, day)
        )
    except (ConfigError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Read only when the runner asked for it. The log is append-only and
    # outlives a config edit, so a runner that trialled the stage and turned it
    # off again would otherwise still be gating on last week's verdicts.
    verdict_by_id = (
        read_verdicts(log_path(config.paths.state_dir))
        if config.paywall.enabled
        else {}
    )

    result = select_day(
        response,
        candidates,
        policy=config.policy,
        now=datetime.now(UTC),
        run_date=day,
        verdict_by_id=verdict_by_id,
    )
    path = write_artifact(
        artifact_path(config.paths.state_dir, SELECTION_STAGE, day), result
    )

    print(format_summary(result, path=path))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from curated_feed.config import Adjustments, PolicySettings
from curated_feed.models import (
    VERDICT_FREE,
    VERDICT_PAYWALLED,
    VERDICT_UNKNOWN,
    Candidate,
    Graded,
    Response,
    SelectionResult,
    Verdict,
)
from curated_feed.select import BELOW_FLOOR, PAYWALLED, UNACCOUNTED, select_day

NOW = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
RUN_DATE = date(2026, 8, 9)
FRESH = NOW - timedelta(hours=1)
STALE = NOW - timedelta(hours=72)

FLOOR = 3.0
MAX_ITEMS = 10
PREFERENCE_CAP = 3
TWO_TOPICS = 2
BEYOND_CAP = 2


def make_candidates(count: int, *, published_at: datetime | None = FRESH):
    return [
        Candidate(
            id=f"{number:016d}",
            url=f"https://example.com/{number}",
            title=f"Title {number}",
            summary="A summary.",
            source="A source",
            source_url="https://example.com/feed.xml",
            folder=None,
            published_at=published_at,
            published_missing=published_at is None,
            fetched_at=NOW,
        )
        for number in range(1, count + 1)
    ]


def select(
    response: Response,
    candidates=None,
    *,
    policy: PolicySettings | None = None,
    verdict_by_id: dict[str, Verdict] | None = None,
) -> SelectionResult:
    if candidates is None:
        candidates = make_candidates(len(response.accounted_indices) or 1)
    return select_day(
        response,
        candidates,
        policy=policy or PolicySettings(),
        now=NOW,
        run_date=RUN_DATE,
        verdict_by_id=verdict_by_id,
    )


def verdicts(candidates, **by_number: str) -> dict[str, Verdict]:
    """A verdict map keyed the way the paywall log is: by candidate id."""
    return {
        candidates[int(number) - 1].id: Verdict(
            candidate_id=candidates[int(number) - 1].id,
            url=candidates[int(number) - 1].url,
            verdict=verdict,
            checked_at=NOW,
        )
        for number, verdict in by_number.items()
    }


def ids(result: SelectionResult) -> list[str]:
    return [item.candidate_id for item in result.selected]


def rejected_reason(result: SelectionResult, candidate_id: str) -> str:
    matches = [
        reason for reason, members in result.rejected.items() if candidate_id in members
    ]
    assert len(matches) == 1, f"{candidate_id} rejected under {matches}"
    return matches[0]


# --------------------------------------------------------------------------- #
# The coverage invariant
# --------------------------------------------------------------------------- #


class TestCoverage:
    def test_an_empty_day_selects_nothing(self):
        result = select(Response(), make_candidates(0))

        assert result.selected == []
        assert result.rejected == {}
        assert result.candidate_count == 0

    def test_every_candidate_is_accounted_for_exactly_once(self):
        response = Response(
            dropped={"no-substance": [1], "excluded:a-rule": [2]},
            graded=[Graded(index=3, consequence=3, duplicates=[4])],
        )
        result = select(response, make_candidates(5))

        accounted = [
            *ids(result),
            *(dup for item in result.selected for dup in item.duplicates),
            *(member for members in result.rejected.values() for member in members),
        ]
        assert sorted(accounted) == [c.id for c in make_candidates(5)]

    def test_a_candidate_the_response_never_mentions_is_recorded(self):
        """The only detector of a model losing its place in a long enumeration."""
        response = Response(graded=[Graded(index=1, consequence=3)])
        result = select(response, make_candidates(3))

        assert result.rejected[UNACCOUNTED] == ["0000000000000002", "0000000000000003"]

    def test_an_index_beyond_the_candidate_list_is_noted_not_crashed(self):
        response = Response(graded=[Graded(index=99, consequence=3)])
        result = select(response, make_candidates(2))

        assert result.selected == []
        assert any("99" in note for note in result.anomalies)


# --------------------------------------------------------------------------- #
# Conflicting claims
# --------------------------------------------------------------------------- #


class TestConflicts:
    def test_a_dropped_article_is_not_resurrected_as_a_grade(self):
        """A hard exclusion is a gate, so the drop wins over any later claim."""
        response = Response(
            dropped={"excluded:a-rule": [1]},
            graded=[Graded(index=1, consequence=3)],
        )
        result = select(response, make_candidates(1))

        assert result.selected == []
        assert rejected_reason(result, "0000000000000001") == "excluded:a-rule"

    def test_a_dropped_article_is_removed_from_a_duplicate_group(self):
        response = Response(
            dropped={"no-substance": [2]},
            graded=[Graded(index=1, consequence=3, duplicates=[2])],
        )
        result = select(response, make_candidates(2))

        assert result.selected[0].duplicates == []
        assert rejected_reason(result, "0000000000000002") == "no-substance"

    def test_an_article_claimed_by_two_groups_joins_only_the_first(self):
        response = Response(
            graded=[
                Graded(index=1, consequence=3, duplicates=[3]),
                Graded(index=2, consequence=3, duplicates=[3]),
            ],
        )
        result = select(response, make_candidates(3))

        assert [item.duplicates for item in result.selected] == [
            ["0000000000000003"],
            [],
        ]

    def test_a_graded_article_is_not_also_someone_elses_duplicate(self):
        response = Response(
            graded=[
                Graded(index=1, consequence=3, duplicates=[2]),
                Graded(index=2, consequence=3),
            ],
        )
        result = select(response, make_candidates(2))

        assert ids(result) == ["0000000000000001", "0000000000000002"]
        assert result.selected[0].duplicates == []


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #


class TestScore:
    def test_consequence_alone_can_clear_the_floor(self):
        """What keeps the system from decaying into a topic filter."""
        result = select(Response(graded=[Graded(index=1, consequence=3, topics=[])]))

        assert result.selected[0].score == pytest.approx(FLOOR)

    def test_topics_add_one_each(self):
        result = select(
            Response(graded=[Graded(index=1, consequence=1, topics=["a", "b"])])
        )

        assert result.selected[0].preference == TWO_TOPICS

    def test_topic_matches_are_capped(self):
        graded = Graded(index=1, consequence=1, topics=["a", "b", "c", "d", "e"])
        result = select(Response(graded=[graded]))

        assert result.selected[0].preference == PREFERENCE_CAP

    def test_the_same_topic_twice_counts_once(self):
        graded = Graded(index=1, consequence=3, topics=["Privacy", "privacy"])
        result = select(Response(graded=[graded]))

        assert result.selected[0].preference == 1

    def test_an_unrecognised_topic_still_counts(self):
        """Topic names are not validated; inflation is a policy problem."""
        graded = Graded(index=1, consequence=2, topics=["something invented"])
        result = select(Response(graded=[graded]))

        assert result.selected[0].preference == 1

    def test_independent_recurrence_adds_one(self):
        graded = Graded(index=1, consequence=2, independent_sources=2)
        result = select(Response(graded=[graded]))

        assert ("recurrence", 1.0) in [
            (a.reason, a.value) for a in result.selected[0].adjustments
        ]

    def test_one_outlet_earns_no_recurrence(self):
        graded = Graded(index=1, consequence=3, independent_sources=1)
        result = select(Response(graded=[graded]))

        assert result.selected[0].adjustments == []

    def test_recurrence_scales_between_one_source_and_the_cap(self):
        """Below the cap, extra sources earn partial credit, not the full bonus."""
        policy = PolicySettings(adjustments=Adjustments(recurrence_cap_sources=3))
        graded = Graded(index=1, consequence=3, independent_sources=2)
        result = select(Response(graded=[graded]), policy=policy)

        assert ("recurrence", pytest.approx(0.5)) in [
            (a.reason, a.value) for a in result.selected[0].adjustments
        ]

    def test_recurrence_reaches_full_weight_at_the_cap(self):
        policy = PolicySettings(adjustments=Adjustments(recurrence_cap_sources=3))
        graded = Graded(index=1, consequence=3, independent_sources=3)
        result = select(Response(graded=[graded]), policy=policy)

        assert ("recurrence", pytest.approx(1.0)) in [
            (a.reason, a.value) for a in result.selected[0].adjustments
        ]

    def test_each_penalty_costs_one(self):
        graded = Graded(
            index=1,
            consequence=3,
            topics=["a", "b", "c"],
            penalties=["chrome", "windows-routine"],
        )
        result = select(Response(graded=[graded]))
        item = result.selected[0]

        assert [(a.reason, a.value) for a in item.adjustments] == [
            ("penalty:chrome", -1.0),
            ("penalty:windows-routine", -1.0),
        ]
        assert item.score == pytest.approx(3 + PREFERENCE_CAP - TWO_TOPICS)

    def test_an_old_article_is_penalised(self):
        graded = Graded(index=1, consequence=3)
        result = select(
            Response(graded=[graded]), make_candidates(1, published_at=STALE)
        )

        assert result.selected == []

    def test_durable_analysis_is_exempt_from_age(self):
        graded = Graded(index=1, consequence=3, durable=True)
        result = select(
            Response(graded=[graded]), make_candidates(1, published_at=STALE)
        )

        assert ids(result) == ["0000000000000001"]

    def test_a_missing_date_is_not_penalised(self):
        graded = Graded(index=1, consequence=3)
        result = select(
            Response(graded=[graded]), make_candidates(1, published_at=None)
        )

        assert ids(result) == ["0000000000000001"]


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


class TestAssembly:
    def test_everything_below_the_floor_selects_nothing(self):
        response = Response(
            graded=[Graded(index=number, consequence=1) for number in (1, 2, 3)]
        )
        result = select(response, make_candidates(3))

        assert result.selected == []
        assert sorted(result.rejected["below-floor"]) == [
            c.id for c in make_candidates(3)
        ]

    def test_items_are_ranked_by_score(self):
        response = Response(
            graded=[
                Graded(index=1, consequence=3),
                Graded(index=2, consequence=3, topics=["a", "b"]),
                Graded(index=3, consequence=3, topics=["a"]),
            ]
        )
        result = select(response, make_candidates(3))

        assert ids(result) == [
            "0000000000000002",
            "0000000000000003",
            "0000000000000001",
        ]
        assert [item.rank for item in result.selected] == [1, 2, 3]

    def test_the_count_is_capped(self):
        count = MAX_ITEMS + 2
        response = Response(
            graded=[
                Graded(index=number, consequence=3) for number in range(1, count + 1)
            ]
        )
        result = select(response, make_candidates(count))

        assert len(result.selected) == MAX_ITEMS
        assert len(result.rejected["over-cap"]) == BEYOND_CAP

    def test_a_discretionary_pick_bypasses_the_floor(self):
        graded = Graded(index=1, consequence=1, discretionary=True)
        result = select(Response(graded=[graded]))

        assert ids(result) == ["0000000000000001"]
        assert result.selected[0].discretionary

    def test_only_one_discretionary_pick_survives(self):
        response = Response(
            graded=[
                Graded(index=1, consequence=1, discretionary=True),
                Graded(index=2, consequence=2, discretionary=True),
            ]
        )
        result = select(response, make_candidates(2))

        assert ids(result) == ["0000000000000002"]
        assert rejected_reason(result, "0000000000000001") == "below-floor"
        assert result.anomalies


# --------------------------------------------------------------------------- #
# What gets published
# --------------------------------------------------------------------------- #


class TestRationale:
    def test_consequence_and_preference_are_always_reported(self):
        graded = Graded(index=1, consequence=3, topics=["a", "b"])
        result = select(Response(graded=[graded]))

        assert result.selected[0].rationale == "Consequence 3 of 3. Preference +2."

    def test_zero_preference_is_reported_without_a_sign(self):
        result = select(Response(graded=[Graded(index=1, consequence=3)]))

        assert result.selected[0].rationale == "Consequence 3 of 3. Preference 0."

    def test_several_outlets_are_reported(self):
        graded = Graded(index=1, consequence=3, independent_sources=3)
        result = select(Response(graded=[graded]))

        assert "3" in result.selected[0].rationale

    def test_staleness_is_reported(self):
        graded = Graded(index=1, consequence=3, discretionary=True)
        result = select(
            Response(graded=[graded]), make_candidates(1, published_at=STALE)
        )

        assert "Older than the window (-1)" in result.selected[0].rationale

    def test_no_unvalidated_model_text_reaches_the_rationale(self):
        """D5: privacy-safe by construction, not by a denylist.

        Topics and penalty rule names are both free text a model wrote about
        the reader's own interests, unlike consequence, preference, the fixed
        adjustment reasons, and `discretionary_reason` — which describes the
        article, never the reader, and is the one piece of model text D5
        allows through.
        """
        graded = Graded(
            index=1,
            consequence=3,
            topics=["Something Personal"],
            penalties=["chrome"],
        )
        result = select(Response(graded=[graded]))

        assert "Personal" not in result.selected[0].rationale
        assert "chrome" not in result.selected[0].rationale

    def test_a_discretionary_pick_reports_its_reason(self):
        graded = Graded(
            index=1,
            consequence=1,
            discretionary=True,
            discretionary_reason="an unfamiliar angle on a familiar story",
        )
        result = select(Response(graded=[graded]))

        assert (
            "Discretionary pick: an unfamiliar angle on a familiar story."
            in result.selected[0].rationale
        )

    def test_a_discretionary_pick_without_a_reason_is_still_flagged(self):
        graded = Graded(index=1, consequence=1, discretionary=True)
        result = select(Response(graded=[graded]))

        assert "Discretionary pick." in result.selected[0].rationale

    def test_a_non_discretionary_pick_reports_nothing_extra(self):
        graded = Graded(index=1, consequence=3)
        result = select(Response(graded=[graded]))

        assert "Discretionary" not in result.selected[0].rationale

    def test_rationales_can_be_turned_off(self):
        graded = Graded(index=1, consequence=3, independent_sources=3)
        result = select(
            Response(graded=[graded]), policy=PolicySettings(rationale_mode="none")
        )

        assert result.selected[0].rationale == ""


class TestThePaywallGate:
    """Unreadable is worth nothing at any score, so it is a gate, not a penalty.

    A -1 adjustment would let a major story the reader cannot open outscore one
    they can, which is exactly backwards: the article is worth nothing to them
    whatever it is about.
    """

    def test_a_paywalled_article_is_not_published(self):
        candidates = make_candidates(2)
        response = Response(
            graded=[Graded(index=1, consequence=3), Graded(index=2, consequence=3)]
        )
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_PAYWALLED}),
        )

        assert ids(result) == [candidates[1].id]

    def test_it_is_recorded_under_its_own_reason(self):
        """`_check_coverage` requires it, and tuning needs to see the count."""
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=3)])
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_PAYWALLED}),
        )

        assert result.rejected[PAYWALLED] == [candidates[0].id]

    def test_it_is_not_mistaken_for_a_near_miss(self):
        """Landing under `below-floor` would misreport why the day was short."""
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=3)])
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_PAYWALLED}),
        )

        assert BELOW_FLOOR not in result.rejected

    def test_its_duplicates_are_still_accounted_for(self):
        candidates = make_candidates(2)
        response = Response(graded=[Graded(index=1, consequence=3, duplicates=[2])])
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_PAYWALLED}),
        )

        assert rejected_reason(result, candidates[1].id).startswith("duplicate-of:")

    def test_the_gate_runs_before_the_cap_so_the_next_article_takes_the_slot(self):
        """Gating after selection would leave a short feed with a queue behind it."""
        candidates = make_candidates(2)
        policy = PolicySettings(max_items=1)
        response = Response(
            graded=[Graded(index=1, consequence=3), Graded(index=2, consequence=3)]
        )
        result = select(
            response,
            candidates,
            policy=policy,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_PAYWALLED}),
        )

        assert ids(result) == [candidates[1].id]

    def test_a_discretionary_pick_does_not_bypass_it(self):
        """Hard gates are gates. An unopenable article is unopenable."""
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=1, discretionary=True)])
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_PAYWALLED}),
        )

        assert ids(result) == []


class TestVerdictsThatDoNotGate:
    """`unknown` is not a guess, and it never shrinks the feed."""

    def test_an_unknown_verdict_is_treated_as_readable(self):
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=3)])
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_UNKNOWN}),
        )

        assert ids(result) == [candidates[0].id]

    def test_a_free_verdict_publishes(self):
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=3)])
        result = select(
            response,
            candidates,
            verdict_by_id=verdicts(candidates, **{"1": VERDICT_FREE}),
        )

        assert ids(result) == [candidates[0].id]

    def test_a_candidate_with_no_verdict_publishes(self):
        """The cap can truncate the probe list, and that must not gate anything."""
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=3)])

        assert ids(select(response, candidates, verdict_by_id={})) == [candidates[0].id]

    def test_no_verdicts_at_all_selects_exactly_as_before(self):
        """Every day already saved in the corpus replays unchanged."""
        candidates = make_candidates(1)
        response = Response(graded=[Graded(index=1, consequence=3)])

        assert select(response, candidates) == select(
            response, candidates, verdict_by_id=None
        )

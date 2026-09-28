from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from curated_feed import paywall
from curated_feed.config import Config, PaywallSettings
from curated_feed.models import (
    VERDICT_FREE,
    VERDICT_PAYWALLED,
    VERDICT_UNKNOWN,
    Candidate,
    Graded,
    Response,
    Verdict,
)
from curated_feed.paywall import check_all, detect, probe_order, read_verdicts
from curated_feed.state import append_jsonl

# Every page here is written for this suite. No real article's markup is
# copied in, and every host is under `.invalid` — see the module docstring in
# `tests/test_integration.py` for why that matters.
PAGES = Path(__file__).parent / "fixtures" / "paywall"


def page(name: str) -> str:
    return (PAGES / f"{name}.html").read_text(encoding="utf-8")


class TestDetectingARestriction:
    """`isAccessibleForFree` is the whole signal, in three carriers."""

    @pytest.mark.parametrize(
        "name",
        [
            "jsonld-paywalled",
            "jsonld-graph",
            "jsonld-malformed",
            "microdata-content",
            "microdata-text",
            "meta-name",
            "schema-url-false",
        ],
    )
    def test_a_flagged_page_is_paywalled(self, name: str):
        assert detect(page(name)) == VERDICT_PAYWALLED

    def test_a_graph_is_walked_rather_than_read_at_the_top_level(self):
        """The marker is on one node of `@graph`, not on the document."""
        assert detect(page("jsonld-graph")) == VERDICT_PAYWALLED

    def test_a_broken_block_does_not_hide_a_valid_one(self):
        """Publishers ship more than one block, and not all of them parse."""
        assert detect(page("jsonld-malformed")) == VERDICT_PAYWALLED


class TestAbsenceMeansFree:
    """Publishers mark the restriction, never the absence of one.

    This is the half that decides most pages, and reading it the other way
    round would gate almost every article in the feed.
    """

    @pytest.mark.parametrize("name", ["jsonld-free", "jsonld-silent", "no-markup"])
    def test_an_unflagged_page_is_free(self, name: str):
        assert detect(page(name)) == VERDICT_FREE

    def test_markup_that_is_present_and_true_is_free(self):
        assert detect(page("jsonld-free")) == VERDICT_FREE

    def test_a_page_with_no_markup_at_all_is_free(self):
        assert detect(page("no-markup")) == VERDICT_FREE


class TestDetectIsTotal:
    """It answers for any string it is given; `unknown` is decided at the edge.

    Whether a page was *reached* is the transport's business, so `detect` has
    only two answers and never raises — a page that cannot be parsed is a page
    carrying no restriction.
    """

    @pytest.mark.parametrize(
        "html",
        [
            "",
            "not html at all",
            "<html><head><script type='application/ld+json'>[[[</script></head></html>",
            "<html><body><span itemprop='isAccessibleForFree'></span></body></html>",
        ],
    )
    def test_it_returns_free_rather_than_raising(self, html: str):
        assert detect(html) == VERDICT_FREE


# %%
# Probing


NOW = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)

# Enough candidates to see an order, and few enough to read in a failure.
CONSEQUENCES = (1, 3, 2)
CAPPED_TO_ONE = 1


def make_candidate(number: int, *, name: str = "no-markup") -> Candidate:
    """A candidate whose URL names the fixture the fake server should serve."""
    return Candidate(
        id=f"{number:016x}",
        url=f"https://reader.invalid/{name}",
        title=f"Article {number}",
        summary="A summary.",
        source="A source",
        source_url="https://reader.invalid/feed.xml",
        folder=None,
        published_at=NOW,
        published_missing=False,
        fetched_at=NOW,
    )


class PageServer:
    """Serves the fixture named by the path, and counts what was asked for."""

    def __init__(self, *, status: int = 200, content_type: str = "text/html"):
        self.status = status
        self.content_type = content_type
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.lstrip("/")
        self.requested.append(name)
        if self.status != httpx.codes.OK:
            return httpx.Response(self.status, text="<html>Refused.</html>")
        return httpx.Response(
            self.status,
            text=page(name),
            headers={"content-type": self.content_type},
        )


@pytest.fixture
def serve_pages(monkeypatch: pytest.MonkeyPatch):
    """Puts a fake network in place of the real one, and hands back the server.

    Every fixture URL is under `.invalid`, so a substitution that failed to
    take would fail to resolve rather than reach anything — but it would also
    read as `unknown`, which this stage treats as readable, and the test would
    pass quietly. The request count is what closes that hole: assert on it.
    """

    def install(server: PageServer) -> PageServer:
        monkeypatch.setattr(
            paywall,
            "build_client",
            lambda settings, *, user_agent: httpx.AsyncClient(
                transport=httpx.MockTransport(server),
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            ),
        )
        return server

    return install


def make_config(tmp_path: Path, **paywall_settings) -> Config:
    """A config whose paths exist and whose paywall section is under test."""
    return Config(
        paths={
            "sources": tmp_path / "sources.opml",
            "corpus_dir": tmp_path / "corpus",
            "policy": tmp_path / "policy.md",
            "state_dir": tmp_path / "state",
            "build_dir": tmp_path / "build",
        },
        paywall=PaywallSettings(enabled=True, **paywall_settings),
    )


class TestProbeOrder:
    """Most consequential first, so a cap truncates the least important."""

    def test_candidates_are_ordered_by_consequence_descending(self):
        candidates = [make_candidate(number) for number in range(1, 4)]
        response = Response(
            graded=[
                Graded(index=number, consequence=consequence)
                for number, consequence in enumerate(CONSEQUENCES, start=1)
            ]
        )
        ordered = probe_order(response, candidates)

        assert [item.id for item in ordered] == [
            candidates[1].id,
            candidates[2].id,
            candidates[0].id,
        ]

    def test_ungraded_candidates_are_never_probed(self):
        """The ~150 the model drops as no-substance are not worth a request."""
        candidates = [make_candidate(number) for number in range(1, 4)]
        response = Response(graded=[Graded(index=2, consequence=1)])

        assert [item.id for item in probe_order(response, candidates)] == [
            candidates[1].id
        ]

    def test_an_index_that_does_not_exist_is_skipped(self):
        """The response is untrusted here too, and selection resolves it later."""
        candidates = [make_candidate(1)]
        response = Response(
            graded=[Graded(index=9, consequence=3), Graded(index=1, consequence=1)]
        )

        assert [item.id for item in probe_order(response, candidates)] == [
            candidates[0].id
        ]


class TestCheckAll:
    def test_a_flagged_page_is_paywalled(self, tmp_path: Path, serve_pages):
        server = serve_pages(PageServer())
        verdicts, _ = check_all(
            [make_candidate(1, name="jsonld-paywalled")],
            make_config(tmp_path),
            now=NOW,
        )

        assert server.requested == ["jsonld-paywalled"]
        assert verdicts[0].verdict == VERDICT_PAYWALLED

    def test_an_unflagged_page_is_free(self, tmp_path: Path, serve_pages):
        server = serve_pages(PageServer())
        verdicts, _ = check_all(
            [make_candidate(1, name="no-markup")], make_config(tmp_path), now=NOW
        )

        assert server.requested == ["no-markup"]
        assert verdicts[0].verdict == VERDICT_FREE

    def test_a_refusal_is_unknown_rather_than_a_wall(self, tmp_path: Path, serve_pages):
        """A site that refuses robots has said nothing about its paywall."""
        serve_pages(PageServer(status=httpx.codes.FORBIDDEN))
        verdicts, _ = check_all([make_candidate(1)], make_config(tmp_path), now=NOW)

        assert verdicts[0].verdict == VERDICT_UNKNOWN
        assert "403" in verdicts[0].detail

    def test_a_refusal_is_not_retried(self, tmp_path: Path, serve_pages):
        """Otherwise the same site is hammered again every morning, forever."""
        server = serve_pages(PageServer(status=httpx.codes.FORBIDDEN))
        check_all([make_candidate(1)], make_config(tmp_path, max_retries=3), now=NOW)

        assert len(server.requested) == 1

    def test_a_body_that_is_not_html_is_unknown(self, tmp_path: Path, serve_pages):
        serve_pages(PageServer(content_type="application/pdf"))
        verdicts, _ = check_all([make_candidate(1)], make_config(tmp_path), now=NOW)

        assert verdicts[0].verdict == VERDICT_UNKNOWN

    def test_the_cap_truncates_and_says_how_much(self, tmp_path: Path, serve_pages):
        """A silent truncation would read as full coverage."""
        server = serve_pages(PageServer())
        candidates = [make_candidate(number) for number in range(1, 4)]
        verdicts, dropped = check_all(
            candidates, make_config(tmp_path, max_checks=CAPPED_TO_ONE), now=NOW
        )

        assert len(server.requested) == CAPPED_TO_ONE
        assert len(verdicts) == CAPPED_TO_ONE
        assert dropped == len(candidates) - CAPPED_TO_ONE

    def test_the_user_agent_falls_back_to_the_fetch_one(
        self, tmp_path: Path, serve_pages
    ):
        """One contact URL for the whole runner is the usual case."""
        config = make_config(tmp_path)
        server = PageServer()
        captured: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers["user-agent"])
            return server(request)

        serve_pages(record)
        check_all([make_candidate(1)], config, now=NOW)

        assert captured == [config.fetch.user_agent]


class TestVerdictsAreCached:
    def test_a_missing_log_reads_as_nothing_known(self, tmp_path: Path):
        assert read_verdicts(tmp_path / "absent.jsonl") == {}

    def test_verdicts_come_back_keyed_by_candidate_id(self, tmp_path: Path):
        path = tmp_path / "paywall.jsonl"
        verdict = Verdict(
            candidate_id="abc",
            url="https://reader.invalid/a",
            verdict=VERDICT_PAYWALLED,
            checked_at=NOW,
        )
        append_jsonl(path, [verdict])

        assert read_verdicts(path)["abc"].verdict == VERDICT_PAYWALLED

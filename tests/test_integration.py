"""The whole chain, from a subscription list to a rendered feed.

Every other test in this suite stops at one stage's edge. This one runs
`curate-run` exactly as CI does — fetch, score, paywall, select, render — with
three things substituted and nothing else: the HTTP client serves the fixture
feeds through a mock transport, the paywall stage's client serves article pages
through another, and the Anthropic client is the same fake `test_claude.py`
uses.
Everything between those two points is the real code, including the parts that
only ever meet in a full run: canonicalisation feeding deduplication, the
corpus's order becoming the candidate numbers the model answers with, and the
scoring retry.

Nothing here can reach the network. Every host in the fixtures is under
`.invalid`, which is reserved precisely so that it cannot resolve, so a
substitution that failed to take fails the test rather than dialling out.

Dates are interpolated at request time rather than baked into the fixtures. The
lookback window is measured from the moment the stage runs and `fetch.main`
takes no clock, so a fixed timestamp would be inside the window today and
outside it next week.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from curated_feed import claude, fetch, paywall
from curated_feed.cli import main
from curated_feed.models import MAX_CONSEQUENCE, Graded, Response
from curated_feed.policy import default_prompt_path
from curated_feed.render import ATOM_NAMESPACE
from curated_feed.score import NO_SUBSTANCE
from fakes import FakeClient, make_message

FIXTURES = Path(__file__).parent / "fixtures" / "integration"
PATH_PREFIX = "a-long-random-segment"

ALPHA = "https://alpha.example.invalid/rss"
BETA = "https://beta.example.invalid/atom"
GAMMA = "https://gamma.example.invalid/rss"

ALPHA_ONE = "https://alpha.example.invalid/one"
ALPHA_OLD = "https://alpha.example.invalid/old"
ALPHA_TWO = "https://alpha.example.invalid/two"
BETA_ONE = "https://beta.example.invalid/one"
BETA_TWO = "https://beta.example.invalid/two"
GAMMA_ONE = "https://gamma.example.invalid/one"

# How many `{tN}` slots the fixtures interpolate, newest first.
RECENT_SLOTS = 6
STALE_DAYS = 30

# Alpha's first item, whose description arrives as HTML with a space in front of
# its full stop. It is the one the plain-text and the published-summary tests
# both read, at opposite ends of the chain.
ALPHA_ONE_SUMMARY = "The court held that fair use did not apply."
ALPHA_ONE_NUMBER = 2

# Candidate numbers are positions in the corpus, which is sorted newest first.
# The fixtures assign the timestamps that produce this order.
CANDIDATES = (BETA_ONE, ALPHA_ONE, GAMMA_ONE, ALPHA_TWO, BETA_TWO)
CANDIDATE_COUNT = len(CANDIDATES)

# Alpha's article, syndicated by Gamma, is one record; Alpha's third item is a
# month old. Both are outside the corpus, for different reasons.
WITHOUT_GAMMA = 4

# A response that fails validation: candidate numbers start at one.
INVALID_RESPONSE = '{"dropped": {}, "graded": [{"index": 0, "consequence": 3}]}'

ATTEMPTS_WITH_RETRY = 2

CONFIG = f"""
[paths]
sources = "sources.opml"
corpus_dir = "corpus"
policy = "editorial-policy.md"
state_dir = "state"
build_dir = "build"

[fetch]
lookback_days = 2
concurrency = 3
# No retries, so a test that serves a failure does not wait out the backoff.
max_retries = 0

[score]
provider = "anthropic"
model = "claude-opus-5"
effort = ""

[publish]
window = 100
title = "A feed"
author = "nobody"
site_url = "https://example.invalid"
path_prefix = "{PATH_PREFIX}"
"""


# The same instance with the paywall stage switched on. Appended rather than
# interpolated, so the config every other test uses is untouched.
PAYWALL_CONFIG = f"""{CONFIG}
[paywall]
enabled = true
concurrency = 2
max_retries = 0
"""


# Two runners in one instance. Their paths are what keeps them apart, so every
# written location below carries the runner's name — exactly as the committed
# configs do.
RUNNERS = ("one", "two")

RUNNER_CONFIG = """
[paths]
sources = "sources.opml"
corpus_dir = "../../corpus/{name}"
policy = "editorial-policy.md"
state_dir = "../../state/{name}"
build_dir = "../../build/{name}"

[fetch]
lookback_days = 2
concurrency = 3
max_retries = 0

[score]
provider = "anthropic"
model = "claude-opus-5"
effort = ""

[publish]
window = 100
title = "A feed"
author = "nobody"
site_url = "https://{name}.example.invalid"
path_prefix = "{prefix}"
"""


def runner_prefix(name: str) -> str:
    """One runner's segment under its own build directory.

    >>> runner_prefix("one")
    'one-segment'
    """
    return f"{name}-segment"


# --------------------------------------------------------------------------- #
# What the model would have said
# --------------------------------------------------------------------------- #


def graded_response(*chosen: int, total: int = CANDIDATE_COUNT) -> str:
    """A day's judgements: `chosen` graded through the floor, the rest dropped.

    Built from the real models rather than written out, so that a fixture cannot
    quietly stop being a response the pipeline would accept. Consequence at the
    ceiling and marked durable is what clears the floor without depending on the
    adjustments, exactly as the stub scorer does.
    """
    rest = [number for number in range(1, total + 1) if number not in chosen]
    return Response(
        dropped={NO_SUBSTANCE: rest} if rest else {},
        graded=[
            Graded(index=number, consequence=MAX_CONSEQUENCE, durable=True)
            for number in chosen
        ],
    ).model_dump_json(indent=2)


TWO_GOOD_ARTICLES = (1, 3)  # Beta's first and Gamma's, by corpus position.


# --------------------------------------------------------------------------- #
# The feeds, served without a socket
# --------------------------------------------------------------------------- #


def _rfc822(moment: datetime) -> str:
    """RSS dates, which are RFC 822."""
    return format_datetime(moment)


def _iso(moment: datetime) -> str:
    """Atom dates, which are RFC 3339."""
    return moment.isoformat()


FEED_FILES: dict[str, tuple[str, Callable[[datetime], str]]] = {
    ALPHA: ("feed-alpha.xml", _rfc822),
    BETA: ("feed-beta.xml", _iso),
    GAMMA: ("feed-gamma.xml", _rfc822),
}


class FeedServer:
    """Serves the fixture feeds, and records what was asked for.

    A URL that is not one of the three raises rather than returning a 404: the
    subscription list names exactly these, so an unrecognised request is a
    mistake in the test and should read as one.
    """

    def __init__(self, *, broken: Collection[str] = ()):
        self.broken = set(broken)
        self.requested: list[str] = []
        self.now = datetime.now(UTC)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in self.broken:
            return httpx.Response(500)

        name, format_date = FEED_FILES[url]
        body = (FIXTURES / name).read_text(encoding="utf-8")
        return httpx.Response(
            200,
            content=body.format(**self._dates(format_date)).encode("utf-8"),
            headers={"content-type": "application/xml"},
        )

    def _dates(self, format_date: Callable[[datetime], str]) -> dict[str, str]:
        """`t1` is the newest; `stale` is outside any sensible window."""
        moments = {
            f"t{slot}": self.now - timedelta(hours=slot)
            for slot in range(1, RECENT_SLOTS + 1)
        }
        moments["stale"] = self.now - timedelta(days=STALE_DAYS)
        return {name: format_date(moment) for name, moment in moments.items()}


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Outcome:
    """What a run left behind, and what it asked of the three fakes."""

    code: int
    feeds: FeedServer
    client: FakeClient
    pages: PageServer


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """A complete instance: config, policy documents and a subscription list."""
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "editorial-policy.md").write_text(
        "# Policy\n\nWhat matters.\n", encoding="utf-8"
    )
    shutil.copy(FIXTURES / "sources.opml", tmp_path / "sources.opml")
    return tmp_path


class PageServer:
    """Serves article pages, flagging the URLs it was told are subscriber-only.

    It also counts what it was asked for. That count is what proves the
    substitution took: an unsubstituted client would fail to resolve `.invalid`,
    the stage would call that `unknown`, and `unknown` publishes — so the test
    would pass while checking nothing at all.
    """

    def __init__(self, paywalled: Collection[str] = ()):
        self.paywalled = set(paywalled)
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        marker = "false" if url in self.paywalled else "true"
        return httpx.Response(
            httpx.codes.OK,
            text=(
                '<html><head><script type="application/ld+json">'
                f'{{"@type": "NewsArticle", "isAccessibleForFree": {marker}}}'
                "</script></head><body><p>An article.</p></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )


@pytest.fixture
def substitutes(monkeypatch: pytest.MonkeyPatch):
    """Puts the fake network and the fake model in place of the real ones.

    A fixture of its own because a run is no longer the only caller: a second
    runner installs its own pair, and the two must not share a reply queue.
    """

    def install(
        *,
        replies: Iterable[object] = (),
        broken: Collection[str] = (),
        paywalled: Collection[str] = (),
    ) -> tuple[FeedServer, FakeClient, PageServer]:
        feeds = FeedServer(broken=broken)
        pages = PageServer(paywalled=paywalled)
        client = FakeClient(
            *(replies or [make_message(graded_response(*TWO_GOOD_ARTICLES))])
        )
        monkeypatch.setattr(
            fetch,
            "build_client",
            lambda settings: httpx.AsyncClient(
                transport=httpx.MockTransport(feeds),
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            ),
        )
        monkeypatch.setattr(
            paywall,
            "build_client",
            lambda settings, *, user_agent: httpx.AsyncClient(
                transport=httpx.MockTransport(pages),
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            ),
        )
        monkeypatch.setattr(claude, "build_client", lambda timeout_seconds: client)
        return feeds, client, pages

    return install


@pytest.fixture
def curate(instance: Path, substitutes):
    """Runs `curate-run` with the network and the model replaced."""

    def run(
        *extra: str,
        replies: Iterable[object] = (),
        broken: Collection[str] = (),
        paywalled: Collection[str] = (),
    ) -> Outcome:
        feeds, client, pages = substitutes(
            replies=replies, broken=broken, paywalled=paywalled
        )
        code = main(["--config", str(instance / "config.toml"), *extra])
        return Outcome(code=code, feeds=feeds, client=client, pages=pages)

    return run


@pytest.fixture
def instances(tmp_path: Path) -> Path:
    """Two runners under one `feeds/`, sharing nothing but the packaged prompt.

    Neither names that prompt: it ships with the engine, so it is the one
    document a runner cannot get wrong. No `config.toml` sits above them either:
    every command here is given one explicitly, and a discoverable config would
    let a run that ignored `--config` pass anyway.
    """
    for name in RUNNERS:
        directory = tmp_path / "feeds" / name
        directory.mkdir(parents=True)
        (directory / "config.toml").write_text(
            RUNNER_CONFIG.format(name=name, prefix=runner_prefix(name)),
            encoding="utf-8",
        )
        (directory / "editorial-policy.md").write_text(
            f"# Policy\n\nWhat {name} cares about.\n", encoding="utf-8"
        )
        shutil.copy(FIXTURES / "sources.opml", directory / "sources.opml")
    return tmp_path


@pytest.fixture
def curate_runner(instances: Path, substitutes):
    """Runs one runner end to end, as its own matrix job would."""

    def run(name: str, *extra: str, replies: Iterable[object] = ()) -> Outcome:
        feeds, client, pages = substitutes(replies=replies)
        config = instances / "feeds" / name / "config.toml"
        code = main(["--config", str(config), *extra])
        return Outcome(code=code, feeds=feeds, client=client, pages=pages)

    return run


def corpus_records(instance: Path) -> list[dict]:
    """Today's corpus, in the order the fetch stage wrote it."""
    path = next(iter((instance / "corpus").glob("*.jsonl")))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def entries_at(feed_path: Path) -> list[ET.Element]:
    """The entries of one rendered feed, in the order it published them."""
    feed = ET.fromstring(  # noqa: S314 - our own output
        feed_path.read_text(encoding="utf-8")
    )
    return feed.findall(f"{{{ATOM_NAMESPACE}}}entry")


def feed_entries(instance: Path) -> list[ET.Element]:
    """The entries of the rendered feed, in the order it published them."""
    return entries_at(instance / "build" / PATH_PREFIX / "feed.xml")


def runner_feed_ids(instances: Path, name: str) -> list[str | None]:
    """One runner's published entry ids, read from its own build directory."""
    feed_path = instances / "build" / name / runner_prefix(name) / "feed.xml"
    found = [entry.find(f"{{{ATOM_NAMESPACE}}}id") for entry in entries_at(feed_path)]
    return [None if element is None else element.text for element in found]


def published_urls(instances: Path, name: str) -> set[str]:
    """Every article in one runner's published log."""
    log = instances / "state" / name / "published.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    return {json.loads(line)["url"] for line in lines if line.strip()}


def feed_text(instance: Path, tag: str) -> list[str | None]:
    """One child element's text from every entry, absent elements included."""
    found = [
        entry.find(f"{{{ATOM_NAMESPACE}}}{tag}") for entry in feed_entries(instance)
    ]
    return [None if element is None else element.text for element in found]


def feed_ids(instance: Path) -> list[str | None]:
    """The entry ids, which are the article URLs."""
    return feed_text(instance, "id")


def saved_response(instance: Path) -> str:
    """The raw text of the last attempt, as the audit trail holds it."""
    return next(iter((instance / "state" / "response").glob("*.json"))).read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestTheWholeChain:
    def test_a_day_runs_from_a_subscription_list_to_a_feed(
        self, instance: Path, curate
    ):
        outcome = curate()

        assert outcome.code == 0
        assert [record["url"] for record in corpus_records(instance)] == list(
            CANDIDATES
        )
        assert feed_ids(instance) == [BETA_ONE, GAMMA_ONE]

    def test_every_stage_leaves_its_artifact(self, instance: Path, curate):
        curate()
        state = instance / "state"

        assert next(iter((instance / "corpus").glob("*.jsonl"))).is_file()
        assert next(iter((state / "response").glob("*.json"))).is_file()
        assert next(iter((state / "selections").glob("*.json"))).is_file()
        assert (state / "published.jsonl").is_file()
        assert (state / "feed.json").is_file()

    def test_the_candidate_numbers_the_model_answers_with_are_corpus_positions(
        self, instance: Path, curate
    ):
        """The one contract between the two halves, and it is never written down.

        Grading a number that pointed at a different article would still produce
        a valid feed, so nothing else in the suite would notice.
        """
        outcome = curate(replies=[make_message(graded_response(ALPHA_ONE_NUMBER))])

        assert outcome.code == 0
        assert feed_ids(instance) == [ALPHA_ONE]

    def test_the_source_description_reaches_the_published_feed(
        self, instance: Path, curate
    ):
        """The whole point of the chain's last two stages carrying it.

        The rationale line always follows the description now, reporting
        consequence and preference even when neither adjustment applies.
        """
        curate(replies=[make_message(graded_response(ALPHA_ONE_NUMBER))])

        assert feed_text(instance, "summary") == [
            f"{ALPHA_ONE_SUMMARY}\n\nConsequence 3 of 3. Preference 0."
        ]

    def test_the_prompt_carries_both_policy_documents(self, curate):
        """The runner's editorial policy, and the engine's own scoring prompt.

        The second is asserted against the packaged file rather than a fixture,
        because nothing in an instance can supply it: if packaging ever stops
        carrying it, this is the test that says so.
        """
        outcome = curate()
        system = outcome.client.requests[0]["system"][0]["text"]

        assert "What matters." in system
        assert default_prompt_path().read_text(encoding="utf-8").strip() in system

    def test_a_named_scoring_prompt_replaces_the_packaged_one(
        self, instance: Path, curate
    ):
        """The one way a prompt change can be tried without reinstalling.

        Once the corpus and the engine live in different repositories this is
        how a candidate prompt is evaluated against a saved day, so it is worth
        a test that runs the whole chain rather than one that calls the
        assembler directly.
        """
        candidate = instance / "candidate-prompt.md"
        candidate.write_text("# Candidate\n\nJudge differently.\n", encoding="utf-8")

        outcome = curate("--scoring-prompt", str(candidate))
        system = outcome.client.requests[0]["system"][0]["text"]

        assert "Judge differently." in system
        assert "What matters." in system
        assert "# Scoring Prompt" not in system


class TestFetching:
    def test_only_the_subscribed_feeds_are_requested(self, curate):
        assert sorted(curate().feeds.requested) == sorted([ALPHA, BETA, GAMMA])

    def test_the_same_article_from_two_feeds_is_one_record(
        self, instance: Path, curate
    ):
        """Gamma syndicates Alpha's article with tracking parameters appended."""
        curate()
        urls = [record["url"] for record in corpus_records(instance)]

        assert urls.count(ALPHA_ONE) == 1
        assert not [url for url in urls if "utm_" in url]

    def test_an_item_outside_the_window_never_reaches_the_corpus(
        self, instance: Path, curate
    ):
        curate()
        urls = [record["url"] for record in corpus_records(instance)]

        assert ALPHA_OLD not in urls

    def test_a_folder_is_recorded_and_a_loose_feed_has_none(
        self, instance: Path, curate
    ):
        curate()
        folders = {
            record["url"]: record["folder"] for record in corpus_records(instance)
        }

        assert folders[BETA_ONE] == "Research"
        assert folders[ALPHA_ONE] is None

    def test_a_summary_reaches_the_corpus_as_plain_text(self, instance: Path, curate):
        """Alpha's first item is HTML, with a space in front of its full stop."""
        curate()
        summaries = {
            record["url"]: record["summary"] for record in corpus_records(instance)
        }

        assert summaries[ALPHA_ONE] == ALPHA_ONE_SUMMARY

    def test_a_broken_feed_does_not_fail_the_day(
        self, instance: Path, curate, capsys: pytest.CaptureFixture
    ):
        outcome = curate(
            replies=[make_message(graded_response(1, total=WITHOUT_GAMMA))],
            broken=[GAMMA],
        )

        assert outcome.code == 0
        assert len(corpus_records(instance)) == WITHOUT_GAMMA
        assert feed_ids(instance) == [BETA_ONE]
        assert GAMMA in capsys.readouterr().out


class TestScoring:
    def test_a_fenced_response_is_accepted(self, instance: Path, curate):
        """Wrapping JSON in a fence is a trained habit, not an error."""
        fenced = (
            f"Here is the day.\n\n```json\n{graded_response(*TWO_GOOD_ARTICLES)}\n```\n"
        )

        outcome = curate(replies=[make_message(fenced)])

        assert outcome.code == 0
        assert feed_ids(instance) == [BETA_ONE, GAMMA_ONE]

    def test_what_arrived_is_what_is_saved(self, instance: Path, curate):
        """The artifact is the audit trail, so it keeps the fence and the preamble."""
        fenced = (
            f"Here is the day.\n\n```json\n{graded_response(*TWO_GOOD_ARTICLES)}\n```\n"
        )

        curate(replies=[make_message(fenced)])

        assert saved_response(instance) == fenced

    def test_an_invalid_response_is_retried(self, instance: Path, curate):
        outcome = curate(
            replies=[
                make_message(INVALID_RESPONSE),
                make_message(graded_response(*TWO_GOOD_ARTICLES)),
            ]
        )

        assert outcome.code == 0
        assert len(outcome.client.requests) == ATTEMPTS_WITH_RETRY
        assert feed_ids(instance) == [BETA_ONE, GAMMA_ONE]

    def test_the_retry_carries_the_validation_error(self, curate):
        """The correction has to survive the whole way to the wire."""
        outcome = curate(
            replies=[
                make_message(INVALID_RESPONSE),
                make_message(graded_response(*TWO_GOOD_ARTICLES)),
            ]
        )
        first, second = (
            request["messages"][0]["content"] for request in outcome.client.requests
        )

        assert len(first) == 1
        assert "graded.0.index" in second[-1]["text"]

    def test_a_second_invalid_response_publishes_nothing(self, instance: Path, curate):
        """A failed day costs one missing day; the served feed stays as it was."""
        outcome = curate(
            replies=[make_message(INVALID_RESPONSE), make_message(INVALID_RESPONSE)]
        )

        assert outcome.code != 0
        assert not (instance / "build").exists()
        assert saved_response(instance) == INVALID_RESPONSE


class TestRunningAgain:
    def test_a_second_run_the_same_day_changes_nothing(self, instance: Path, curate):
        """The corpus deduplicates and the entry ids are the article URLs."""
        curate()
        feed_path = instance / "build" / PATH_PREFIX / "feed.xml"
        first = feed_path.read_text(encoding="utf-8")
        published = (instance / "state" / "published.jsonl").read_text(encoding="utf-8")

        curate()

        assert len(corpus_records(instance)) == CANDIDATE_COUNT
        assert feed_path.read_text(encoding="utf-8") == first
        assert (instance / "state" / "published.jsonl").read_text(
            encoding="utf-8"
        ) == published


class TestTwoRunners:
    """Two runners in one instance, which is what a directory per feed buys.

    Nothing in `src/` knows there is more than one: the partition is entirely in
    the locations each config declares. So what is worth proving is the negative
    — that running one leaves nothing of itself in the other, neither an entry in
    its log nor a file in its state directory.
    """

    def test_each_publishes_its_own_selection(self, instances: Path, curate_runner):
        first = curate_runner("one")
        second = curate_runner(
            "two", replies=[make_message(graded_response(ALPHA_ONE_NUMBER))]
        )

        assert (first.code, second.code) == (0, 0)
        assert runner_feed_ids(instances, "one") == [BETA_ONE, GAMMA_ONE]
        assert runner_feed_ids(instances, "two") == [ALPHA_ONE]

    def test_neither_published_log_holds_the_others_entries(
        self, instances: Path, curate_runner
    ):
        """The log is the one artifact that is appended to rather than rewritten.

        Two runners sharing it would publish each other's picks into both feeds,
        and the feeds would still render, which is why this is checked.
        """
        curate_runner("one")
        curate_runner("two", replies=[make_message(graded_response(ALPHA_ONE_NUMBER))])

        assert published_urls(instances, "one") == {BETA_ONE, GAMMA_ONE}
        assert published_urls(instances, "two") == {ALPHA_ONE}

    def test_running_one_writes_nothing_for_the_other(
        self, instances: Path, curate_runner
    ):
        outcome = curate_runner("one")

        assert outcome.code == 0
        assert (instances / "state" / "one" / "feed.json").is_file()
        assert not (instances / "state" / "two").exists()
        assert not (instances / "corpus" / "two").exists()
        assert not (instances / "build" / "two").exists()


# --------------------------------------------------------------------------- #
# Checking that the picks can be opened
# --------------------------------------------------------------------------- #


@pytest.fixture
def paywall_instance(instance: Path) -> Path:
    """The same instance, with the paywall stage switched on."""
    (instance / "config.toml").write_text(PAYWALL_CONFIG, encoding="utf-8")
    return instance


@pytest.fixture
def curate_checked(paywall_instance: Path, substitutes):
    """Runs the chain for a runner that checks whether its picks can be read."""

    def run(*extra: str, paywalled: Collection[str] = ()) -> Outcome:
        feeds, client, pages = substitutes(paywalled=paywalled)
        code = main(["--config", str(paywall_instance / "config.toml"), *extra])
        return Outcome(code=code, feeds=feeds, client=client, pages=pages)

    return run


class TestThePaywallStage:
    """The stage in place, in a real run, with a real feed coming out.

    `TWO_GOOD_ARTICLES` are Beta's first and Gamma's, so gating one of them
    leaves the other — which is the behaviour worth proving end to end.
    """

    def test_only_the_graded_articles_are_probed(
        self, paywall_instance: Path, curate_checked
    ):
        """Not the ones dropped for having no substance. Somebody's server."""
        outcome = curate_checked()

        assert sorted(outcome.pages.requested) == sorted([BETA_ONE, GAMMA_ONE])

    def test_a_paywalled_article_never_reaches_the_feed(
        self, paywall_instance: Path, curate_checked
    ):
        outcome = curate_checked(paywalled=[BETA_ONE])

        assert outcome.code == 0
        assert feed_ids(paywall_instance) == [GAMMA_ONE]

    def test_a_readable_article_still_does(
        self, paywall_instance: Path, curate_checked
    ):
        curate_checked()

        assert feed_ids(paywall_instance) == [BETA_ONE, GAMMA_ONE]

    def test_the_verdicts_are_written_where_selection_looks(
        self, paywall_instance: Path, curate_checked
    ):
        curate_checked(paywalled=[BETA_ONE])
        log = paywall_instance / "state" / "paywall.jsonl"
        verdicts = {
            json.loads(line)["url"]: json.loads(line)["verdict"]
            for line in log.read_text(encoding="utf-8").splitlines()
        }

        assert verdicts == {BETA_ONE: "paywalled", GAMMA_ONE: "free"}

    def test_a_second_run_probes_nothing_again(
        self, paywall_instance: Path, curate_checked
    ):
        """The whole point of the log: nobody's server is asked twice."""
        curate_checked()
        today = datetime.now(UTC).date().isoformat()
        outcome = curate_checked("--from", "paywall", "--date", today)

        assert outcome.pages.requested == []
        assert outcome.code == 0

    def test_a_runner_that_did_not_ask_probes_nothing(self, instance: Path, curate):
        """Off by default, and off means no requests at all."""
        outcome = curate()

        assert outcome.pages.requested == []
        assert feed_ids(instance) == [BETA_ONE, GAMMA_ONE]

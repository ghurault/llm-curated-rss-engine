"""Deciding whether a candidate's page can actually be read.

The signal is schema.org's `isAccessibleForFree`, and it is the only one. It is
reliable because Google requires it of publishers who want paywalled content
indexed, which is why it is on the page at all.

Read it in one direction only: **present and false means paywalled, absent
means free.** Publishers mark the restriction, not the absence of one, so
treating silence as evidence of a wall would gate almost every article in the
feed.

Two things that look like signals and are not, so nobody adds them:

- **Body length.** There is no cross-site threshold. A truncated article from a
  long-form magazine outruns a complete one from a wire service, so length
  measures house style rather than access.
- **The RSS subscriber marker.** Where it exists at all it is channel
  boilerplate, and nothing survives `build_record` normalisation. Detection
  without a request is not available.

`detect` is pure and total: it answers for any string, and it has two answers.
Whether a page was *reached* is the transport's business, and that is where the
third verdict is decided.

The stage runs after scoring, so it probes the graded records rather than every
candidate: the ones the model dropped for having no substance are not worth a
request to anybody's server. Verdicts are appended to a log keyed by candidate
id and never revisited, so replaying a day costs nothing and a re-run does not
knock on the same door twice.

This fetches a full article page, and it must not be read as delivering the
reading-effort scoring `docs/ARCHITECTURE.md` defers. It looks at the markup and
throws the body away.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .config import Config, ConfigError, PaywallSettings, load_config
from .fetch import RETRY_STATUS_CODES, retry_delay
from .models import (
    VERDICT_FREE,
    VERDICT_PAYWALLED,
    VERDICT_UNKNOWN,
    Candidate,
    Response,
    Verdict,
)
from .score import RESPONSE_STAGE, read_response
from .state import (
    StateError,
    append_jsonl,
    artifact_path,
    read_corpus,
    read_jsonl,
    resolve_corpus,
)

PAYWALL_LOG = "paywall.jsonl"

ACCESSIBLE_KEY = "isAccessibleForFree"

# Publishers emit all of these for the same thing, including the two URL forms
# schema.org itself defines. Compared casefolded, so `False` needs no entry.
FALSE_VALUES = frozenset(
    {
        "false",
        "no",
        "0",
        "http://schema.org/false",
        "https://schema.org/false",
    }
)

JSON_LD_TYPE = "application/ld+json"
GRAPH_KEY = "@graph"


def detect(html: str) -> str:
    """Whether a page's markup restricts it, in order of how publishers carry it.

    >>> detect('<script type="application/ld+json">{"isAccessibleForFree": false}</script>')
    'paywalled'
    >>> detect('<meta name="isAccessibleForFree" content="no">')
    'paywalled'
    >>> detect("<html><body><p>An article.</p></body></html>")
    'free'
    """
    soup = BeautifulSoup(html, "html.parser")
    for value in _markers(soup):
        if _is_false(value):
            return VERDICT_PAYWALLED
    return VERDICT_FREE


def _markers(soup: BeautifulSoup) -> list[Any]:
    """Every `isAccessibleForFree` the page carries, in any of its three forms."""
    return [*_from_json_ld(soup), *_from_microdata(soup), *_from_meta(soup)]


def _from_json_ld(soup: BeautifulSoup) -> list[Any]:
    """The usual carrier, and the fiddliest: nested, repeated, and often broken.

    A block that does not parse is skipped rather than fatal — publishers ship
    several, and one of them having a trailing comma must not hide the rest.
    """
    found: list[Any] = []
    for script in soup.find_all("script", attrs={"type": JSON_LD_TYPE}):
        try:
            document = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        found += _walk(document)
    return found


def _walk(node: Any) -> list[Any]:
    """Collect the marker from anywhere in a JSON-LD document.

    Publishers put the article node at the top level, inside a top-level array,
    or inside `@graph` beside the site and organisation nodes. Walking covers
    all three without a rule per shape.

    >>> _walk({"@graph": [{"isAccessibleForFree": False}]})
    [False]
    """
    if isinstance(node, dict):
        found = [node[ACCESSIBLE_KEY]] if ACCESSIBLE_KEY in node else []
        for key, value in node.items():
            if key != ACCESSIBLE_KEY:
                found += _walk(value)
        return found
    if isinstance(node, list):
        return [marker for item in node for marker in _walk(item)]
    return []


def _from_microdata(soup: BeautifulSoup) -> list[Any]:
    """An `itemprop`, carrying its value in `content` or as its text."""
    found = []
    for element in soup.find_all(attrs={"itemprop": ACCESSIBLE_KEY}):
        content = element.get("content")
        found.append(content if content is not None else element.get_text())
    return found


def _from_meta(soup: BeautifulSoup) -> list[Any]:
    """The plain `<meta name=…>` form, which a few publishers use instead."""
    return [
        element.get("content", "")
        for element in soup.find_all("meta", attrs={"name": ACCESSIBLE_KEY})
    ]


def _is_false(value: Any) -> bool:
    """Whether one marker says the article is restricted.

    Anything unrecognised is not a restriction. The marker is only ever read in
    that direction, so an unexpected value leaves the article readable rather
    than gating it on a guess.

    >>> [_is_false(value) for value in (False, "False", "no", True, "", None)]
    [True, True, True, False, False, False]
    """
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().casefold() in FALSE_VALUES
    return False


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RunSummary:
    """Everything the end-of-run report needs."""

    log_path: Path
    graded: int = 0
    cached: int = 0
    dropped_by_cap: int = 0
    verdicts: list[Verdict] = field(default_factory=list)

    def counted(self, verdict: str) -> int:
        return sum(1 for item in self.verdicts if item.verdict == verdict)


def probe_order(response: Response, candidates: Sequence[Candidate]) -> list[Candidate]:
    """The graded candidates, most consequential first.

    Ordered rather than merely collected so that `max_checks` truncates the
    least important tail. Consequence is a deliberate stand-in for the score:
    ranking properly here would be a second implementation of `select.py`'s
    arithmetic, and the two would drift.

    An index the response invented is skipped. Selection records that as an
    anomaly later; this stage only has to avoid tripping over it.
    """
    ranked = sorted(
        (record for record in response.graded if 1 <= record.index <= len(candidates)),
        key=lambda record: (-record.consequence, record.index),
    )
    return [candidates[record.index - 1] for record in ranked]


def build_client(settings: PaywallSettings, *, user_agent: str) -> httpx.AsyncClient:
    """The client one run probes with.

    A factory, so the stage's one external dependency can be substituted the
    way `fetch.py`'s is. The User-Agent is passed in rather than read from
    `settings`, because an empty one there means "use the runner's", and this
    module is not where that is resolved.
    """
    return httpx.AsyncClient(
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html, application/xhtml+xml, */*",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=httpx.Timeout(settings.timeout_seconds),
        follow_redirects=True,
    )


async def check_one(
    client: httpx.AsyncClient, candidate: Candidate, *, max_retries: int, now: datetime
) -> Verdict:
    """Fetch one article page and read its markup.

    Every failure is `unknown` rather than an exception: a page that cannot be
    reached has said nothing about whether it can be read, and this stage must
    never cost the day its feed.
    """
    detail = "unknown error"
    for attempt in range(max_retries + 1):
        try:
            page = await client.get(candidate.url)
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}: {exc}".strip().splitlines()[0]
        else:
            if page.is_error:
                detail = f"HTTP {page.status_code}"
                # A refusal is settled. Retrying it would knock on the same
                # closed door every morning for as long as the runner lives.
                if page.status_code not in RETRY_STATUS_CODES:
                    break
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay(page, attempt))
                    continue
            elif "html" not in page.headers.get("content-type", "").lower():
                detail = f"not HTML: {page.headers.get('content-type', 'no type')}"
                break
            else:
                return _verdict(candidate, detect(page.text), now=now)

        if attempt < max_retries:
            await asyncio.sleep(2.0**attempt)

    return _verdict(candidate, VERDICT_UNKNOWN, now=now, detail=detail)


def _verdict(
    candidate: Candidate, verdict: str, *, now: datetime, detail: str = ""
) -> Verdict:
    return Verdict(
        candidate_id=candidate.id,
        url=candidate.url,
        verdict=verdict,
        checked_at=now,
        detail=detail,
    )


async def _check_every(
    candidates: Sequence[Candidate], config: Config, *, now: datetime
) -> list[Verdict]:
    """Probe with bounded concurrency. These are article pages: stay modest."""
    settings = config.paywall
    limiter = asyncio.Semaphore(settings.concurrency)
    user_agent = settings.user_agent or config.fetch.user_agent

    async with build_client(settings, user_agent=user_agent) as client:

        async def guarded(candidate: Candidate) -> Verdict:
            async with limiter:
                return await check_one(
                    client, candidate, max_retries=settings.max_retries, now=now
                )

        return list(await asyncio.gather(*(guarded(item) for item in candidates)))


def check_all(
    candidates: Sequence[Candidate], config: Config, *, now: datetime
) -> tuple[list[Verdict], int]:
    """Probe up to `max_checks` candidates, and say how many were left unprobed.

    The count is returned rather than logged away because a cap that truncates
    quietly reads as full coverage, and the day it matters is the day nobody is
    looking.
    """
    capped = list(candidates[: config.paywall.max_checks])
    if not capped:
        return [], len(candidates)
    return asyncio.run(_check_every(capped, config, now=now)), len(candidates) - len(
        capped
    )


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #


def log_path(state_dir: Path) -> Path:
    """Where a runner's verdicts accumulate.

    >>> log_path(Path("/state")).as_posix()
    '/state/paywall.jsonl'
    """
    return state_dir / PAYWALL_LOG


def read_verdicts(path: Path) -> dict[str, Verdict]:
    """Every verdict recorded so far, by candidate id.

    A missing file reads as an empty map, which is what makes the whole feature
    a no-op until a runner turns it on, and what lets every day already saved in
    the corpus replay exactly as it did.

    A candidate probed twice keeps its later verdict, which is the one a repair
    run would have written.
    """
    return {record.candidate_id: record for record in read_jsonl(path, Verdict)}


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def collect(config: Config, *, day, candidates, response, now: datetime) -> RunSummary:
    """Probe what has not been probed before, and append what was learned."""
    path = log_path(config.paths.state_dir)
    known = read_verdicts(path)

    graded = probe_order(response, candidates)
    fresh = [item for item in graded if item.id not in known]

    verdicts, dropped = check_all(fresh, config, now=now)
    append_jsonl(path, verdicts)

    return RunSummary(
        log_path=path,
        graded=len(graded),
        cached=len(graded) - len(fresh),
        dropped_by_cap=dropped,
        verdicts=verdicts,
    )


def format_summary(summary: RunSummary) -> str:
    """Human-readable run report."""
    lines = [
        "",
        f"Graded          : {summary.graded}",
        f"  already known : {summary.cached}",
        f"Probed          : {len(summary.verdicts)}",
        f"  free          : {summary.counted(VERDICT_FREE)}",
        f"  paywalled     : {summary.counted(VERDICT_PAYWALLED)}",
        f"  unknown       : {summary.counted(VERDICT_UNKNOWN)}",
    ]
    lines += [
        f"    - {item.url}: {item.detail}"
        for item in summary.verdicts
        if item.verdict == VERDICT_UNKNOWN
    ]
    if summary.dropped_by_cap:
        lines.append(f"Not probed (cap): {summary.dropped_by_cap}")
    lines.append(f"Verdicts        : {summary.log_path}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-paywall",
        description="Check whether the day's graded articles can be read.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="collection day to check (default: the most recent one)",
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
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not config.paywall.enabled:
        print("Paywall checking is off for this runner; nothing to do")
        return 0

    try:
        corpus_file, day = resolve_corpus(config.paths.corpus_dir, args.date)
        candidates = read_corpus(corpus_file)
        response = read_response(
            artifact_path(config.paths.state_dir, RESPONSE_STAGE, day)
        )
    except (ConfigError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = collect(
        config,
        day=day,
        candidates=candidates,
        response=response,
        now=datetime.now(UTC),
    )
    print(format_summary(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

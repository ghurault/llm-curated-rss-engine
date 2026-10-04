"""Fetch subscribed feeds and append normalised records to the corpus.

One JSONL file per collection day, one record per item. Records are deduplicated
against every corpus file already on disk (by `id`), so the command can run daily
and accumulate without repeating items. A single broken feed never fails the run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import struct_time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import Config, ConfigError, FetchSettings, load_config
from .models import Candidate
from .opml import Feed, OpmlError, parse_opml
from .state import append_corpus, corpus_path, existing_ids

# Query parameters that identify a campaign or a click, not a document. Stripped
# before hashing so the same article shared through two feeds gets one id.
TRACKING_PARAM_PREFIXES = ("utm_", "at_", "pk_", "hsa_", "vero_", "_hs", "mc_")
TRACKING_PARAMS = frozenset(
    {
        "cmpid",
        "dclid",
        "ea_med",
        "fbclid",
        "gbraid",
        "gclid",
        "igshid",
        "mkt_tok",
        "msclkid",
        "ref_src",
        "s_cid",
        "smid",
        "spm",
        "twclid",
        "wbraid",
        "wtmc",
        "yclid",
    }
)

SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?)\]}%])")

DEFAULT_PORTS = {"http": 80, "https": 443}
ID_LENGTH = 16
RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass(slots=True)
class FeedStats:
    """Per-feed accounting, aggregated into the run summary."""

    items_seen: int = 0
    kept: int = 0
    skipped_outside_window: int = 0
    skipped_no_url: int = 0
    missing_date: int = 0


@dataclass(slots=True)
class FetchResult:
    """Outcome of fetching one feed."""

    feed: Feed
    content: bytes | None = None
    error: str | None = None


@dataclass(slots=True)
class RunSummary:
    """Everything the end-of-run report needs."""

    output_path: Path
    attempted: int = 0
    succeeded: int = 0
    failures: list[tuple[Feed, str]] = field(default_factory=list)
    items_seen: int = 0
    skipped_outside_window: int = 0
    skipped_no_url: int = 0
    skipped_duplicate: int = 0
    missing_date: int = 0
    written: int = 0


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Normalise a URL for identity: lowercase host, no fragment, no tracking."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()

    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(key)
    ]

    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def make_id(canonical_url: str) -> str:
    """Stable id for an item: a hash of its canonical URL.

    Feed GUIDs are deliberately ignored — they are inconsistent across sources
    and sometimes change between fetches of the same article.
    """
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:ID_LENGTH]


def html_to_text(value: str) -> str:
    """Strip markup and collapse whitespace."""
    if not value:
        return ""
    # Separate tags with a space so inline elements do not glue words together,
    # then undo the space that leaves in front of trailing punctuation.
    text = BeautifulSoup(value, "html.parser").get_text(separator=" ")
    return SPACE_BEFORE_PUNCTUATION.sub(r"\1", " ".join(text.split()))


def truncate(text: str, limit: int) -> str:
    """Truncate on a word boundary, marking the cut with an ellipsis."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[: limit - 1]
    if " " in head:
        head = head[: head.rindex(" ")]
    return head.rstrip(" ,.;:—-") + "…"


def _to_datetime(parsed: struct_time | None) -> datetime | None:
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def entry_published(entry: dict) -> datetime | None:
    """Best available publication timestamp, or None if the feed gave none."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        moment = _to_datetime(entry.get(key))
        if moment:
            return moment
    return None


def _entry_summary(entry: dict) -> str:
    value = entry.get("summary") or ""
    if not value:
        contents = entry.get("content") or []
        if contents:
            value = contents[0].get("value") or ""
    return value


def build_record(
    entry: dict,
    *,
    feed: Feed,
    source_title: str,
    fetched_at: datetime,
    summary_max_chars: int,
) -> Candidate | None:
    """Normalise one feed entry into a corpus record, or None if unusable."""
    link = (entry.get("link") or "").strip()
    if not link:
        return None

    canonical = canonicalize_url(link)
    published = entry_published(entry)

    return Candidate(
        id=make_id(canonical),
        url=canonical,
        title=html_to_text(entry.get("title") or "") or "(untitled)",
        summary=truncate(html_to_text(_entry_summary(entry)), summary_max_chars),
        source=source_title,
        source_url=feed.xml_url,
        folder=feed.folder,
        published_at=published,
        published_missing=published is None,
        fetched_at=fetched_at,
    )


def records_from_feed(
    content: bytes,
    feed: Feed,
    *,
    fetched_at: datetime,
    cutoff: datetime | None,
    summary_max_chars: int,
) -> tuple[list[Candidate], FeedStats, str | None]:
    """Parse feed bytes into records.

    Returns the records, per-feed stats, and a parse error for feeds that yielded
    nothing usable. Items with no usable date are kept and flagged rather than
    dropped: feeds are inconsistent about dates and silence is not evidence.
    """
    parsed = feedparser.parse(content)
    entries = parsed.get("entries") or []
    if not entries:
        if parsed.get("bozo"):
            exception = parsed.get("bozo_exception")
            return [], FeedStats(), f"unparsable feed ({type(exception).__name__})"
        return [], FeedStats(), "feed contained no items"

    source_title = html_to_text(parsed.get("feed", {}).get("title") or "") or feed.title

    records: list[Candidate] = []
    stats = FeedStats(items_seen=len(entries))
    for entry in entries:
        published = entry_published(entry)
        if published is None:
            stats.missing_date += 1
        elif cutoff is not None and published < cutoff:
            stats.skipped_outside_window += 1
            continue

        record = build_record(
            entry,
            feed=feed,
            source_title=source_title,
            fetched_at=fetched_at,
            summary_max_chars=summary_max_chars,
        )
        if record is None:
            stats.skipped_no_url += 1
            continue
        records.append(record)

    stats.kept = len(records)
    return records, stats, None


def sort_records(records: Iterable[Candidate]) -> list[Candidate]:
    """Newest first, undated items last, ties broken by title for determinism."""
    return sorted(records, key=_sort_key)


def _sort_key(record: Candidate) -> tuple[bool, float, str]:
    if record.published_at is None:
        return (True, 0.0, record.title)
    return (False, -record.published_at.timestamp(), record.title)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


async def fetch_one(
    client: httpx.AsyncClient, feed: Feed, *, max_retries: int
) -> FetchResult:
    """Fetch one feed, retrying transient failures with a short backoff."""
    last_error = "unknown error"
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(feed.xml_url)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}".strip().splitlines()[0]
        else:
            if not response.is_error:
                return FetchResult(feed=feed, content=response.content)
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUS_CODES:
                break
            if attempt < max_retries:
                await asyncio.sleep(retry_delay(response, attempt))
                continue

        if attempt < max_retries:
            await asyncio.sleep(2.0**attempt)

    return FetchResult(feed=feed, error=last_error)


def retry_delay(response: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying, honouring `Retry-After` when it is sane.

    Public because the paywall stage retries on the same terms, and two copies
    of a backoff policy is one more than anybody will remember to keep in step.
    """
    header = response.headers.get("retry-after", "")
    try:
        return min(float(header), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return 2.0**attempt


def build_client(settings: FetchSettings) -> httpx.AsyncClient:
    """The client one run makes its requests with.

    A factory rather than a client built inline, so that this module's one
    external dependency can be substituted the way every other one in `src/`
    can — `deploy.py` does the same for the command it runs. A test serves the
    subscription list's feeds through a mock transport and the whole stage runs
    without a socket.

    >>> build_client(FetchSettings()).headers["user-agent"]
    'curated-feed/0.1'
    """
    return httpx.AsyncClient(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=httpx.Timeout(settings.timeout_seconds),
        follow_redirects=True,
    )


async def fetch_all(feeds: Sequence[Feed], config: Config) -> list[FetchResult]:
    """Fetch every feed with bounded concurrency."""
    limiter = asyncio.Semaphore(config.fetch.concurrency)

    async with build_client(config.fetch) as client:

        async def guarded(feed: Feed) -> FetchResult:
            async with limiter:
                return await fetch_one(
                    client, feed, max_retries=config.fetch.max_retries
                )

        return list(await asyncio.gather(*(guarded(feed) for feed in feeds)))


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def collect(feeds: Sequence[Feed], config: Config, *, now: datetime) -> RunSummary:
    """Fetch, normalise, deduplicate and write one day of records."""
    lookback_days = config.fetch.lookback_days
    cutoff = now - timedelta(days=lookback_days) if lookback_days > 0 else None
    output_path = corpus_path(config.paths.corpus_dir, now.date())
    summary = RunSummary(output_path=output_path, attempted=len(feeds))

    seen_ids = existing_ids(config.paths.corpus_dir)
    results = asyncio.run(fetch_all(feeds, config))

    fresh: list[Candidate] = []
    for result in results:
        if result.content is None:
            summary.failures.append((result.feed, result.error or "unknown error"))
            continue

        records, stats, parse_error = records_from_feed(
            result.content,
            result.feed,
            fetched_at=now,
            cutoff=cutoff,
            summary_max_chars=config.fetch.summary_max_chars,
        )
        if parse_error:
            summary.failures.append((result.feed, parse_error))
            continue

        summary.succeeded += 1
        summary.items_seen += stats.items_seen
        summary.skipped_outside_window += stats.skipped_outside_window
        summary.skipped_no_url += stats.skipped_no_url
        summary.missing_date += stats.missing_date

        for record in records:
            if record.id in seen_ids:
                summary.skipped_duplicate += 1
                continue
            seen_ids.add(record.id)
            fresh.append(record)

    summary.written = append_corpus(output_path, sort_records(fresh))
    return summary


def format_summary(summary: RunSummary) -> str:
    """Human-readable run report."""
    lines = [
        "",
        f"Feeds attempted : {summary.attempted}",
        f"Feeds succeeded : {summary.succeeded}",
        f"Feeds failed    : {len(summary.failures)}",
    ]
    for feed, reason in summary.failures:
        lines.append(f"  - {feed.title} <{feed.xml_url}>: {reason}")

    lines += [
        f"Items seen      : {summary.items_seen}",
        f"  outside window: {summary.skipped_outside_window}",
        f"  duplicates    : {summary.skipped_duplicate}",
        f"  no URL        : {summary.skipped_no_url}",
        f"  undated (kept): {summary.missing_date}",
        f"Items written   : {summary.written} -> {summary.output_path}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-fetch",
        description=(
            "Fetch subscribed feeds into a JSONL corpus file for today. "
            "A feed that fails is reported in the summary, not fatal, and an "
            "item already in any corpus file is skipped."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--sources", type=Path, help="path to the OPML subscription list"
    )
    parser.add_argument(
        "--corpus-dir", type=Path, help="directory to write corpus files into"
    )
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="lookback window in days, filtered on publication date (0 disables filtering)",
    )
    parser.add_argument(
        "--summary-max-chars", type=int, help="truncate summaries to this length"
    )
    parser.add_argument(
        "--concurrency", type=int, help="maximum concurrent HTTP requests"
    )
    parser.add_argument(
        "--user-agent", help="User-Agent header sent with every request"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config).with_overrides(
            paths={"sources": args.sources, "corpus_dir": args.corpus_dir},
            fetch={
                "lookback_days": args.days,
                "summary_max_chars": args.summary_max_chars,
                "concurrency": args.concurrency,
                "user_agent": args.user_agent,
            },
        )
        feeds = parse_opml(config.paths.sources)
    except (ConfigError, OpmlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not feeds:
        print(f"error: no feeds found in {config.paths.sources}", file=sys.stderr)
        return 2

    print(f"Fetching {len(feeds)} feed(s) from {config.paths.sources}")
    summary = collect(feeds, config, now=datetime.now(UTC))
    print(format_summary(summary))

    return 1 if summary.succeeded == 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Rendering the published window as an Atom feed.

A pure function of `state/published.jsonl`: the rolling log of everything ever
published, denormalised so that rendering cannot be changed by a later edit to
the corpus or to a selection.

Four things here are not stylistic choices, because getting any of them wrong
makes a reader resurface items that were already read:

- an entry's `<id>` is the canonical article URL, and never changes;
- an entry's `<updated>` is the original publication date, never the run time;
- the feed's `<updated>` moves only when a content hash moves;
- the same state renders to the same bytes, every time.

The feed itself is public, so its title and author say nothing about the reader.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from .config import Config, ConfigError, PublishSettings, load_config
from .models import (
    TIMESTAMP_FORMAT,
    Candidate,
    FeedMeta,
    PublishedEntry,
    SelectionResult,
)
from .select import SELECTION_STAGE
from .state import (
    StateError,
    append_jsonl,
    artifact_path,
    read_artifact,
    read_corpus,
    read_jsonl,
    resolve_corpus,
    write_artifact,
)

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
PUBLISHED_LOG = "published.jsonl"
FEED_META = "feed.json"
FEED_FILENAME = "feed.xml"
INDEX_FILENAME = "index.html"
ROBOTS_FILENAME = "robots.txt"
HEADERS_FILENAME = "_headers"
FALLBACK_FEED_ID = "urn:curated-feed"

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

# Read by Cloudflare Pages, and by Netlify, which invented the format: each rule
# is a path pattern followed by indented headers. `robots.txt` asks a crawler not
# to fetch; this tells the ones that fetch anyway not to index, which is the half
# that matters when privacy rests on the URL staying unguessable.
HEADERS = "/*\n  X-Robots-Tag: noindex, nofollow\n"


# --------------------------------------------------------------------------- #
# The published log
# --------------------------------------------------------------------------- #


def to_entries(
    selection: SelectionResult, candidates: Sequence[Candidate]
) -> list[PublishedEntry]:
    """The day's selection as feed entries, in rank order."""
    by_id = {candidate.id: candidate for candidate in candidates}
    entries = []
    for item in selection.selected:
        candidate = by_id.get(item.candidate_id)
        if candidate is None:
            raise StateError(
                f"selected article {item.candidate_id} is not in the corpus"
            )
        entries.append(
            PublishedEntry(
                candidate_id=candidate.id,
                url=candidate.url,
                title=candidate.title,
                summary=candidate.summary,
                source=candidate.source,
                published_at=candidate.published_at,
                rationale=item.rationale,
                run_date=selection.run_date,
            )
        )
    return entries


def unpublished(
    entries: Sequence[PublishedEntry], published: Sequence[PublishedEntry]
) -> list[PublishedEntry]:
    """The entries not already in the log, keyed by the id the feed uses.

    Re-running a day must not publish it twice, and the entry `<id>` is the
    canonical URL, so that is what identity means here.
    """
    seen = {entry.url for entry in published}
    return [entry for entry in entries if entry.url not in seen]


def window(entries: Sequence[PublishedEntry], size: int) -> list[PublishedEntry]:
    """The newest `size` entries, newest first.

    A reader polls on its own schedule and can miss items in a file that churns
    too fast, so the feed carries far more than one day.
    """
    ordered = sorted(entries, key=_entry_order)
    return ordered[:size]


def _entry_order(entry: PublishedEntry) -> tuple[float, str]:
    """Newest first, ties broken by url so a render is reproducible."""
    moment = entry.published_at or datetime.combine(
        entry.run_date, datetime.min.time(), tzinfo=UTC
    )
    return (-moment.timestamp(), entry.url)


# --------------------------------------------------------------------------- #
# Atom
# --------------------------------------------------------------------------- #


def entry_updated(entry: PublishedEntry) -> str:
    """An entry's timestamp: its publication date, never the time of the run."""
    moment = entry.published_at or datetime.combine(
        entry.run_date, datetime.min.time(), tzinfo=UTC
    )
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def summary_text(description: str, rationale: str) -> str:
    r"""The text one entry publishes: the source's description, then the note.

    Both go in `<summary>` rather than one each in `<summary>` and `<content>`,
    because that is the element every reader is certain to show. Splitting them
    would put the description — the only text worth skimming — behind whether a
    particular reader renders content at all.

    The description comes first because it is what the entry is about; the note
    is a sentence of provenance and reads as a footer. Either can be empty: an
    article whose feed gave no description, and a selection whose rationale mode
    is `none`, are both ordinary.

    >>> summary_text("A panel rejected the argument.", "Reported by 3 outlets.")
    'A panel rejected the argument.\n\nReported by 3 outlets.'
    >>> summary_text("A panel rejected the argument.", "")
    'A panel rejected the argument.'
    >>> summary_text("", "")
    ''
    """
    parts = (description.strip(), rationale.strip())
    return "\n\n".join(part for part in parts if part)


def feed_url(publish: PublishSettings) -> str:
    """Where the feed will be served from, or a stable urn if that is unset.

    >>> feed_url(PublishSettings(site_url="https://example.com", path_prefix="abc"))
    'https://example.com/abc/feed.xml'
    """
    if not publish.site_url:
        return FALLBACK_FEED_ID
    parts = [
        publish.site_url.rstrip("/"),
        publish.path_prefix.strip("/"),
        FEED_FILENAME,
    ]
    return "/".join(part for part in parts if part)


def build_feed(
    entries: Sequence[PublishedEntry], *, publish: PublishSettings, updated: datetime
) -> ET.Element:
    """The Atom document, as an element tree.

    Entries are ordered here rather than trusted to arrive ordered, so that the
    byte-identical guarantee holds for the function and not merely for one caller.
    """
    feed = ET.Element("feed", {"xmlns": ATOM_NAMESPACE})
    ET.SubElement(feed, "title").text = publish.title
    ET.SubElement(feed, "id").text = feed_url(publish)
    ET.SubElement(feed, "updated").text = updated.astimezone(UTC).strftime(
        TIMESTAMP_FORMAT
    )
    ET.SubElement(ET.SubElement(feed, "author"), "name").text = publish.author
    ET.SubElement(feed, "link", {"rel": "self", "href": feed_url(publish)})

    for entry in sorted(entries, key=_entry_order):
        element = ET.SubElement(feed, "entry")
        ET.SubElement(element, "title").text = entry.title
        ET.SubElement(element, "id").text = entry.url
        ET.SubElement(element, "link", {"rel": "alternate", "href": entry.url})
        ET.SubElement(element, "updated").text = entry_updated(entry)
        ET.SubElement(ET.SubElement(element, "author"), "name").text = entry.source
        summary = summary_text(entry.summary, entry.rationale)
        if summary:
            ET.SubElement(element, "summary", {"type": "text"}).text = summary
    return feed


def render_atom(
    entries: Sequence[PublishedEntry], *, publish: PublishSettings, updated: datetime
) -> str:
    """The feed as text. The same entries and timestamp give the same bytes."""
    feed = build_feed(entries, publish=publish, updated=updated)
    ET.indent(feed, space="  ")
    body = ET.tostring(feed, encoding="unicode", short_empty_elements=True)
    return f'<?xml version="1.0" encoding="utf-8"?>\n{body}\n'


def content_hash(entries: Sequence[PublishedEntry], *, publish: PublishSettings) -> str:
    """A hash of everything except the feed timestamp.

    Rendering with a fixed timestamp and hashing the result is what makes the
    hash cover exactly what a change of `<updated>` should follow.
    """
    epoch = datetime.fromtimestamp(0, tz=UTC)
    rendered = render_atom(entries, publish=publish, updated=epoch)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def render_index(entries: Sequence[PublishedEntry], *, publish: PublishSettings) -> str:
    """A plain landing page, so the URL is not a bare 404 to a curious visitor."""
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="robots" content="noindex, nofollow">',
        f"<title>{_escape(publish.title)}</title>",
        "</head>",
        "<body>",
        f"<h1>{_escape(publish.title)}</h1>",
        f'<p><a href="{FEED_FILENAME}">Atom feed</a> — {len(entries)} entries.</p>',
        "</body>",
        "</html>",
    ]
    return "\n".join(lines) + "\n"


def _escape(text: str) -> str:
    """Escape text for HTML.

    >>> _escape('a & <b>')
    'a &amp; &lt;b&gt;'
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# Writing the build
# --------------------------------------------------------------------------- #


def feed_directory(config: Config) -> Path:
    """Where the feed is written: the build directory plus the secret segment."""
    prefix = config.publish.path_prefix.strip("/")
    return config.paths.build_dir / prefix if prefix else config.paths.build_dir


def write_build(
    entries: Sequence[PublishedEntry], *, config: Config, updated: datetime
) -> Path:
    """Write the feed and its companions, and return the feed's path.

    The feed and its landing page sit under the unguessable segment; the two
    files that instruct crawlers sit at the root, which is where a host looks
    for them.
    """
    directory = feed_directory(config)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / FEED_FILENAME
    path.write_text(
        render_atom(entries, publish=config.publish, updated=updated), encoding="utf-8"
    )
    (directory / INDEX_FILENAME).write_text(
        render_index(entries, publish=config.publish), encoding="utf-8"
    )
    (config.paths.build_dir / ROBOTS_FILENAME).write_text(ROBOTS_TXT, encoding="utf-8")
    (config.paths.build_dir / HEADERS_FILENAME).write_text(HEADERS, encoding="utf-8")
    return path


def resolve_updated(
    entries: Sequence[PublishedEntry], *, config: Config, meta_path: Path, now: datetime
) -> tuple[datetime, bool]:
    """The feed timestamp, moved only when the content has actually changed."""
    digest = content_hash(entries, publish=config.publish)
    if meta_path.exists():
        previous = read_artifact(meta_path, FeedMeta)
        if previous.content_hash == digest:
            return previous.updated, False
    write_artifact(meta_path, FeedMeta(content_hash=digest, updated=now))
    return now, True


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-render",
        description="Publish the day's selection into the rolling Atom feed.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="collection day to publish (default: the most recent one)",
    )
    parser.add_argument("--build-dir", type=Path, help="directory to write the feed to")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config).with_overrides(
            paths={"build_dir": args.build_dir}
        )
        corpus_file, day = resolve_corpus(config.paths.corpus_dir, args.date)
        selection = read_artifact(
            artifact_path(config.paths.state_dir, SELECTION_STAGE, day), SelectionResult
        )
        candidates = read_corpus(corpus_file)

        log_path = config.paths.state_dir / PUBLISHED_LOG
        published = read_jsonl(log_path, PublishedEntry)
        fresh = unpublished(to_entries(selection, candidates), published)
        append_jsonl(log_path, fresh)

        entries = window([*published, *fresh], config.publish.window)
        updated, changed = resolve_updated(
            entries,
            config=config,
            meta_path=config.paths.state_dir / FEED_META,
            now=datetime.now(UTC),
        )
        path = write_build(entries, config=config, updated=updated)
    except (ConfigError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"\nNewly published : {len(fresh)}"
        f"\nIn the feed     : {len(entries)} of {len(published) + len(fresh)}"
        f"\nContent         : {'changed' if changed else 'unchanged'}"
        f"\nFeed            : {path}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

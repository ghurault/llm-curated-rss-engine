"""OPML subscription-list parsing.

Feedly (and most readers) export feeds nested inside folder outlines, so the
walk is recursive and records the folder path for each feed. Phase 0 does not
use the folder, but later phases weight and quota by source group.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

FOLDER_SEPARATOR = "/"


class OpmlError(Exception):
    """Raised when the OPML file is missing or cannot be parsed."""


@dataclass(frozen=True, slots=True)
class Feed:
    """A single subscription from the OPML file."""

    title: str
    xml_url: str
    folder: str | None = None


def parse_opml(path: Path | str) -> list[Feed]:
    """Parse an OPML export into feeds, in document order.

    Outlines without an `xmlUrl` are treated as folders and recursed into.
    Feeds appearing more than once (the same `xmlUrl` filed under two folders)
    are kept once, at their first occurrence.
    """
    path = Path(path)
    try:
        # The OPML file is a subscription list the user exports and places here
        # themselves, so it is treated as local input rather than a hostile
        # document; defusedxml is not pulled in for it.
        tree = ET.parse(path)  # noqa: S314
    except FileNotFoundError as exc:
        raise OpmlError(f"OPML file not found: {path}") from exc
    except ET.ParseError as exc:
        raise OpmlError(f"could not parse OPML file {path}: {exc}") from exc

    body = tree.getroot().find("body")
    if body is None:
        raise OpmlError(f"{path}: OPML has no <body> element")

    feeds: list[Feed] = []
    seen: set[str] = set()
    for outline in body:
        _walk(outline, folders=(), feeds=feeds, seen=seen)
    return feeds


def _walk(
    outline: ET.Element,
    *,
    folders: tuple[str, ...],
    feeds: list[Feed],
    seen: set[str],
) -> None:
    if outline.tag != "outline":
        return

    xml_url = (outline.get("xmlUrl") or "").strip()
    label = _label(outline)

    if xml_url:
        if xml_url in seen:
            return
        seen.add(xml_url)
        feeds.append(
            Feed(
                title=label or xml_url,
                xml_url=xml_url,
                folder=FOLDER_SEPARATOR.join(folders) if folders else None,
            )
        )
        return

    # No xmlUrl: a folder (or a decorative outline). Recurse, extending the path.
    child_folders = (*folders, label) if label else folders
    for child in outline:
        _walk(child, folders=child_folders, feeds=feeds, seen=seen)


def _label(outline: ET.Element) -> str:
    for attribute in ("title", "text"):
        value = (outline.get(attribute) or "").strip()
        if value:
            return value
    return ""

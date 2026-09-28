"""Curated feed pipeline.

Phase 0 covers corpus collection only: `opml` (subscription list), `fetch`
(collect and normalise items) and `export` (render a day for review). Later
phases add scoring, selection, rendering and deployment modules alongside them.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("curated-feed")
except PackageNotFoundError:  # pragma: no cover - package not installed
    __version__ = "0.0.0"

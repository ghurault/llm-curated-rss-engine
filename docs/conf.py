"""Sphinx configuration for the engine's documentation.

The Markdown is written to be read on GitHub, so its relative links point at
repository files. Those that are not pages are sent to GitHub, and those to a
file a page includes are sent to that page; see `_rewrite_links`.
"""

import os
import re
from importlib.metadata import version as get_version
from pathlib import Path

from pygments.lexers.special import TextLexer
from sphinx.application import Sphinx

project = "curated-feed"
author = "Guillem Hurault"
copyright = "Guillem Hurault"  # noqa: A001
release = get_version("curated-feed")
version = release

extensions = [
    "autoapi.extension",
    "myst_parser",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

# Static analysis: the package is never imported, so building needs none of its
# dependencies.
autoapi_dirs = ["../src"]
autoapi_add_toctree_entry = False
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
    "imported-members",
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# The pages link to each other's sections, as GitHub renders them.
myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = project
html_last_updated_fmt = "%Y-%m-%d"


# %%
# Repository links

REPOSITORY_URL = "https://github.com/ghurault/llm-curated-rss-engine/blob/main/"
ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "docs"

PAGE_BY_INCLUDED_FILE = {
    ROOT / "README.md": SOURCE_DIR / "index.md",
    ROOT / "CONTRIBUTING.md": SOURCE_DIR / "CONTRIBUTING.md",
}

_LINK = re.compile(r"(\]\()([^)\s#]+)(#[^)\s]*)?(\))")


def _resolve_link(target: str, *, written_in: Path, page: Path) -> str:
    """Where a relative link in `written_in`, shown on `page`, should point."""
    path = (written_in.parent / target).resolve()
    path = PAGE_BY_INCLUDED_FILE.get(path, path)
    if path.suffix == ".md" and path.is_relative_to(SOURCE_DIR):
        return os.path.relpath(path, page.parent)
    if path.exists() and path.is_relative_to(ROOT):
        return REPOSITORY_URL + path.relative_to(ROOT).as_posix()
    return target


def _rewrite_links(text: str, *, written_in: Path, page: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        opening, target, anchor, closing = match.groups()
        if "://" not in target and not target.startswith("mailto:"):
            target = _resolve_link(target, written_in=written_in, page=page)
        return opening + target + (anchor or "") + closing

    return _LINK.sub(replace, text)


def _on_source_read(app: Sphinx, docname: str, source: list[str]) -> None:
    path = Path(app.env.doc2path(docname))
    if path.suffix == ".md":
        source[0] = _rewrite_links(source[0], written_in=path, page=path)


def _on_include_read(
    app: Sphinx, relative_path: Path, parent_docname: str, content: list[str]
) -> None:
    written_in = (Path(app.srcdir) / relative_path).resolve()
    page = Path(app.env.doc2path(parent_docname))
    if written_in.suffix == ".md":
        content[0] = _rewrite_links(content[0], written_in=written_in, page=page)


def setup(app: Sphinx) -> None:
    app.connect("source-read", _on_source_read)
    app.connect("include-read", _on_include_read)
    # GitHub highlights it; Pygments has no lexer for it.
    app.add_lexer("gitignore", TextLexer)

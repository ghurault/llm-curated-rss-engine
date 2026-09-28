"""The two documents a new runner is copied from.

A runner is a directory holding a config, an editorial policy and a subscription
list, and every one of them is written by hand. The two that have a shape worth
writing down travel with the engine rather than sitting beside the runners: the
settings reference is derived from the settings model and is checked against it,
and the policy reference records the conventions the scoring prompt depends on.
Both are the engine's to keep current, so a copy kept next to somebody's feeds
would be the copy that went stale.

They are printed rather than copied, because the engine is installed rather than
checked out wherever a runner is created — an image, a virtual environment, an
editable tree. Nothing reads either file at runtime.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

PACKAGE = "curated_feed"

FILENAME_BY_TEMPLATE = {
    "config": "config.template.toml",
    "policy": "editorial-policy.template.md",
}


# %%
# Reading


def template_path(which: str) -> Path:
    """Where a template is installed.

    >>> template_path("config").name
    'config.template.toml'
    >>> template_path("policy").is_file()
    True
    """
    return Path(str(resources.files(PACKAGE).joinpath(FILENAME_BY_TEMPLATE[which])))


def read_template(which: str) -> str:
    """A template's text, exactly as it ships."""
    return template_path(which).read_text(encoding="utf-8")


# %%
# Command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-template",
        description="Print a template for a new runner, to be redirected into place.",
    )
    parser.add_argument(
        "which",
        choices=sorted(FILENAME_BY_TEMPLATE),
        help="which template to print: the settings reference or the policy one",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Written rather than printed: the output is redirected into a file that is
    # then edited by hand, so it must be the template and not a line more.
    sys.stdout.write(read_template(args.which))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

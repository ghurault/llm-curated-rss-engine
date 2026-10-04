"""The CLI reference, which is rendered from the parsers the commands run."""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import pytest

# Read at collection time, to parametrize over.
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
SCRIPTS = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]

_DIRECTIVE = re.compile(r"```\{argparse\}\n(.*?)```", re.DOTALL)
_OPTION = re.compile(r"^:(\w+):\s*(.+)$", re.MULTILINE)

# %%
# Reading the page


def documented_parsers(text: str) -> dict[str, tuple[str, str]]:
    r"""The `(module, func)` each argparse directive renders, keyed by its `prog`.

    >>> documented_parsers("```{argparse}\n:module: m\n:func: f\n:prog: p\n```")
    {'p': ('m', 'f')}
    """
    parser_by_prog: dict[str, tuple[str, str]] = {}
    for body in _DIRECTIVE.findall(text):
        options = dict(_OPTION.findall(body))
        parser_by_prog[options["prog"]] = (options["module"], options["func"])
    return parser_by_prog


@pytest.fixture
def cli_page(repo_root: Path) -> dict[str, tuple[str, str]]:
    return documented_parsers((repo_root / "docs" / "CLI.md").read_text("utf-8"))


# %%
# Tests


class TestTheCliReference:
    def test_it_documents_every_command_and_nothing_else(self, cli_page):
        """A new console script without a page entry fails here, not in review."""
        assert set(cli_page) == set(SCRIPTS)

    @pytest.mark.parametrize("name", sorted(SCRIPTS))
    def test_each_entry_renders_the_module_the_command_runs(self, name, cli_page):
        module, _ = SCRIPTS[name].split(":")
        assert cli_page[name] == (module, "build_parser")

    @pytest.mark.parametrize("name", sorted(SCRIPTS))
    def test_each_parser_calls_itself_by_its_command_name(self, name):
        """Otherwise `--help` and the page show a usage line nobody can type."""
        module, _ = SCRIPTS[name].split(":")
        assert importlib.import_module(module).build_parser().prog == name

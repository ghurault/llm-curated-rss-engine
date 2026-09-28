"""This repository's own runners, and the two things the engine keeps in step.

Everything that is a runner's business — that its settings load, that the files
they name are there and written, that no two runners name one resource — goes
through `curate-validate`, because after the split that command is all the
config repository will have. Holding these directories to exactly what it holds
them to is what stops the two from drifting.

What is left here is the engine's own: the settings template it ships, against
the settings model it is copied from, and the scheduled workflow's matrix,
against the directories it names.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel

from curated_feed.config import Config
from curated_feed.template import template_path
from curated_feed.validate import validate_directory

CONFIG_NAME = "config.toml"
WORKFLOW = Path(".github") / "workflows" / "daily.yml"

# The matrix's default, which is a JSON array inside a workflow expression.
MATRIX_DEFAULT = re.compile(r"fromJSON\(.*?'(\[.*?\])'", re.DOTALL)


# %%
# Reading the settings a runner declares


def setting_keys(model: type[BaseModel], prefix: str = "") -> set[str]:
    """Every setting a config model holds, as dotted keys.

    >>> from curated_feed.config import DeploySettings
    >>> sorted(setting_keys(DeploySettings))
    ['compatibility_date', 'project', 'provider', 'timeout_seconds']
    """
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        annotation = field.annotation
        dotted = f"{prefix}{name}"
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            keys |= setting_keys(annotation, prefix=f"{dotted}.")
        else:
            keys.add(dotted)
    return keys


def table_keys(table: dict, prefix: str = "") -> set[str]:
    """Every setting a TOML file sets, as dotted keys.

    >>> sorted(table_keys({"a": 1, "b": {"c": 2}}))
    ['a', 'b.c']
    """
    keys: set[str] = set()
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, dict):
            keys |= table_keys(value, prefix=f"{dotted}.")
        else:
            keys.add(dotted)
    return keys


def read_keys(path: Path) -> set[str]:
    """Every setting one config file sets."""
    return table_keys(tomllib.loads(path.read_text(encoding="utf-8")))


def matrix_feeds(workflow: str) -> set[str]:
    r"""The runners the scheduled workflow curates, read out of its matrix.

    The list is hardcoded there on purpose — a matrix read off the filesystem
    would make a stray directory a live runner, curating and publishing whatever
    it found — so the copy is read back here rather than trusted.

    >>> sorted(matrix_feeds("feed: ${{ fromJSON(x && '[\"a\", \"b\"]' || y) }}"))
    ['a', 'b']
    """
    found = MATRIX_DEFAULT.search(workflow)
    if found is None:
        raise AssertionError(f"no matrix default in {WORKFLOW}")
    return set(json.loads(found.group(1)))


# %%
# The instance on disk


@pytest.fixture
def feeds_dir(repo_root: Path) -> Path:
    return repo_root / "feeds"


@pytest.fixture
def runner_dirs(feeds_dir: Path) -> list[Path]:
    """One directory per runner, which is what the workflow's matrix carries."""
    return sorted(path for path in feeds_dir.iterdir() if path.is_dir())


# %%
# Tests


class TestTheRunners:
    def test_they_pass_the_validator(self, feeds_dir: Path):
        """One assertion, because `curate-validate` is the whole checklist.

        Reimplementing any of it here would give this repository a second
        standard, and the config repository — which will have the command and
        not this file — the other one.
        """
        assert validate_directory(feeds_dir) == []


class TestTheSettingsTemplate:
    """Ships with the engine, so this is the repository that keeps it current."""

    def test_it_documents_every_setting(self):
        assert read_keys(template_path("config")) == setting_keys(Config)

    def test_no_runner_sets_a_setting_it_omits(self, runner_dirs: list[Path]):
        documented = read_keys(template_path("config"))
        for path in runner_dirs:
            assert read_keys(path / CONFIG_NAME) <= documented, path.name


class TestTheWorkflowMatrix:
    def test_it_names_every_runner_and_nothing_else(
        self, repo_root: Path, runner_dirs: list[Path]
    ):
        """The one place a runner's name is written down twice.

        A directory missing from the matrix is a feed that silently stops being
        curated; a name in the matrix with no directory fails the job outright,
        which is the harmless half. After the split this workflow is the config
        repository's thin caller, so its shape is that repository's business
        rather than something `curate-validate` should know about.
        """
        workflow = (repo_root / WORKFLOW).read_text(encoding="utf-8")

        assert matrix_feeds(workflow) == {path.name for path in runner_dirs}

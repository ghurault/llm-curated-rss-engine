"""The two documents a new runner is copied from, and the command that prints them."""

from __future__ import annotations

import tomllib

import pytest
from pydantic import BaseModel

from curated_feed.config import Config
from curated_feed.template import FILENAME_BY_TEMPLATE, main, read_template

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
    """Every setting a TOML table sets, as dotted keys.

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


# %%
# Tests


class TestTheTemplatesThatShip:
    @pytest.mark.parametrize("which", sorted(FILENAME_BY_TEMPLATE))
    def test_it_is_installed_beside_the_package(self, which: str):
        """Package data, so an installed engine carries it without a checkout."""
        assert read_template(which).strip()

    def test_the_config_one_is_valid_toml(self):
        tomllib.loads(read_template("config"))

    def test_the_config_one_documents_every_setting(self):
        """The reference every new runner is copied from, so it cannot lag the model."""
        documented = table_keys(tomllib.loads(read_template("config")))

        assert documented == setting_keys(Config)


class TestTheCommand:
    def test_it_prints_the_template_verbatim(self, capsys: pytest.CaptureFixture):
        """Redirected into a new runner's directory, so not a line may be added."""
        assert main(["config"]) == 0

        assert capsys.readouterr().out == read_template("config")

    def test_it_refuses_a_name_it_does_not_know(self):
        with pytest.raises(SystemExit):
            main(["nonsense"])

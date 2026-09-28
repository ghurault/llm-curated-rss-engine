"""The two documents a new runner is copied from, and the command that prints them."""

from __future__ import annotations

import tomllib

import pytest

from curated_feed.template import FILENAME_BY_TEMPLATE, main, read_template


class TestTheTemplatesThatShip:
    @pytest.mark.parametrize("which", sorted(FILENAME_BY_TEMPLATE))
    def test_it_is_installed_beside_the_package(self, which: str):
        """Package data, so an installed engine carries it without a checkout."""
        assert read_template(which).strip()

    def test_the_config_one_is_valid_toml(self):
        tomllib.loads(read_template("config"))


class TestTheCommand:
    def test_it_prints_the_template_verbatim(self, capsys: pytest.CaptureFixture):
        """Redirected into a new runner's directory, so not a line may be added."""
        assert main(["config"]) == 0

        assert capsys.readouterr().out == read_template("config")

    def test_it_refuses_a_name_it_does_not_know(self):
        with pytest.raises(SystemExit):
            main(["nonsense"])

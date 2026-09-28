from __future__ import annotations

from pathlib import Path

import pytest

from curated_feed.config import Config, ConfigError, find_config_file, load_config

LOOKBACK_DAYS = 3
MAX_ITEMS = 8
OVERRIDDEN_DAYS = 7
PAYWALL_CONCURRENCY = 4


@pytest.fixture
def config(fixtures_dir: Path) -> Config:
    return load_config(fixtures_dir / "config.toml")


class TestLoading:
    def test_settings_are_grouped_by_section(self, config: Config):
        assert config.fetch.lookback_days == LOOKBACK_DAYS
        assert config.policy.max_items == MAX_ITEMS
        assert config.policy.adjustments.recurrence == pytest.approx(1.0)

    def test_relative_paths_resolve_against_the_config_file(
        self, config: Config, fixtures_dir: Path
    ):
        assert config.paths.sources == fixtures_dir / "subscriptions.opml"
        assert config.paths.policy == fixtures_dir / "policy.md"

    def test_absolute_paths_are_left_alone(self, tmp_path: Path, fixtures_dir: Path):
        text = (fixtures_dir / "config.toml").read_text(encoding="utf-8")
        path = tmp_path / "config.toml"
        path.write_text(text.replace('"corpus"', '"/srv/corpus"'), encoding="utf-8")
        assert load_config(path).paths.corpus_dir == Path("/srv/corpus")

    def test_a_missing_file_is_reported(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "absent.toml")

    def test_a_missing_setting_names_the_section_and_key(
        self, tmp_path: Path, fixtures_dir: Path
    ):
        text = (fixtures_dir / "config.toml").read_text(encoding="utf-8")
        path = tmp_path / "config.toml"
        path.write_text(text.replace('policy = "policy.md"', ""), encoding="utf-8")
        with pytest.raises(ConfigError, match=r"paths.*policy"):
            load_config(path)

    def test_a_config_that_still_names_the_scoring_prompt_is_rejected(
        self, tmp_path: Path, fixtures_dir: Path
    ):
        """The prompt ships with the engine, so naming one is a stale config.

        Rejected rather than ignored: a runner carried over from before the
        prompt moved would otherwise score against the packaged document while
        its configuration said something else, and say nothing.
        """
        text = (fixtures_dir / "config.toml").read_text(encoding="utf-8")
        path = tmp_path / "config.toml"
        path.write_text(
            text.replace(
                'policy = "policy.md"', 'policy = "policy.md"\nprompt = "p.md"'
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match=r"paths.*prompt"):
            load_config(path)

    def test_malformed_toml_is_reported(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        path.write_text("[paths\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)


class TestPaywallSettings:
    """Off unless a runner opts in: the stage requests other people's pages."""

    def test_it_is_off_by_default(self, config: Config):
        assert config.paywall.enabled is False

    def test_its_defaults_are_polite(self, config: Config):
        assert config.paywall.concurrency == PAYWALL_CONCURRENCY
        assert config.paywall.max_retries <= config.fetch.max_retries

    def test_an_empty_user_agent_is_the_documented_fallback(self, config: Config):
        """Resolved against [fetch] at the call site, not defaulted here."""
        assert config.paywall.user_agent == ""

    def test_an_unknown_setting_is_refused(self, tmp_path: Path, fixtures_dir: Path):
        text = (fixtures_dir / "config.toml").read_text(encoding="utf-8")
        path = tmp_path / "config.toml"
        path.write_text(f"{text}\n[paywall]\ncheck_paywall = true\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="paywall"):
            load_config(path)


class TestFloorInvariant:
    """The floor equals the maximum consequence score, and is not a tunable.

    Raise it and off-topic items become mathematically unreachable, with no
    error anywhere — so it is checked once, loudly, at load.
    """

    @pytest.mark.parametrize("floor", ["4.0", "2.0"])
    def test_a_floor_off_the_consequence_scale_is_refused(
        self, tmp_path: Path, fixtures_dir: Path, floor: str
    ):
        text = (fixtures_dir / "config.toml").read_text(encoding="utf-8")
        path = tmp_path / "config.toml"
        path.write_text(
            text.replace("floor = 3.0", f"floor = {floor}"), encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="floor"):
            load_config(path)


class TestOverrides:
    def test_a_value_replaces_the_configured_one(self, config: Config):
        overridden = config.with_overrides(fetch={"lookback_days": OVERRIDDEN_DAYS})
        assert overridden.fetch.lookback_days == OVERRIDDEN_DAYS

    def test_none_leaves_the_configured_value_alone(self, config: Config):
        overridden = config.with_overrides(fetch={"lookback_days": None})
        assert overridden.fetch.lookback_days == LOOKBACK_DAYS

    def test_untouched_sections_survive(self, config: Config):
        overridden = config.with_overrides(fetch={"concurrency": 1})
        assert overridden.paths.sources == config.paths.sources
        assert overridden.policy.max_items == MAX_ITEMS

    def test_an_unknown_section_is_refused(self, config: Config):
        with pytest.raises(ConfigError, match="unknown"):
            config.with_overrides(nonsense={"key": 1})

    def test_an_unknown_setting_is_refused(self, config: Config):
        with pytest.raises(ConfigError):
            config.with_overrides(fetch={"nonsense": 1})


class TestDiscovery:
    def test_the_config_file_is_found_in_a_parent_directory(self, tmp_path: Path):
        (tmp_path / "config.toml").write_text("", encoding="utf-8")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_config_file(nested) == tmp_path / "config.toml"

    def test_no_config_file_anywhere_is_reported(self, tmp_path: Path):
        with pytest.raises(ConfigError, match=r"config\.toml"):
            find_config_file(tmp_path)

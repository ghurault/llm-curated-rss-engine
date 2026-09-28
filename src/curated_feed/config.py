"""Configuration loading.

Settings are grouped into the same sections the TOML file uses, so a value's
name in code and in the file are one and the same.

All filesystem locations come from the config file (or CLI flags), never from
literals in this package: the policy, corpus and state directories belong to the
deployment, not to the code.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import MAX_CONSEQUENCE

DEFAULT_CONFIG_FILENAME = "config.toml"


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


class Section(BaseModel):
    """Base for every settings section: strict, and immutable once loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(Section):
    """Where everything lives. Relative entries resolve against the config file.

    The scoring prompt is deliberately absent. It is the engine's contract with
    its own parser rather than a runner's choice, so it ships with the package
    and no configuration can name it; `--scoring-prompt` overrides it for the
    length of one command. A config that still carries a `prompt` key is
    rejected rather than ignored, which is what makes an un-migrated runner say
    so instead of quietly scoring against the wrong document.
    """

    sources: Path
    corpus_dir: Path
    policy: Path
    state_dir: Path
    build_dir: Path


class FetchSettings(Section):
    """Collecting candidates. These are other people's servers: stay polite."""

    lookback_days: int = Field(default=2, ge=0)
    summary_max_chars: int = Field(default=500, ge=0)
    concurrency: int = Field(default=8, ge=1)
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    user_agent: str = "curated-feed/0.1"


class PaywallSettings(Section):
    """Checking whether a candidate's page can actually be read.

    Off by default: turning it on makes a request per graded article to
    somebody else's site, so it is a runner's decision rather than a default.
    Deliberately more cautious than `[fetch]` — these are article pages, and
    there are more of them than there are feeds.
    """

    enabled: bool = False
    concurrency: int = Field(default=4, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=1, ge=0)
    max_checks: int = Field(default=100, ge=0)
    user_agent: str = ""


class ScoreSettings(Section):
    """Which scorer runs, and what it is allowed to spend."""

    provider: str = "stub"
    model: str = ""
    max_tokens: int = Field(default=32000, ge=1)
    effort: str = ""
    timeout_seconds: float = Field(default=900.0, gt=0)
    stub_items: int = Field(default=5, ge=0)


class Adjustments(Section):
    """The weights of scoring specification §2.3, and their thresholds."""

    recurrence: float = 1.0
    recurrence_cap_sources: int = Field(default=2, ge=2)
    staleness: float = -1.0
    staleness_hours: int = Field(default=48, ge=0)
    penalty: float = -1.0


class PolicySettings(Section):
    """The arithmetic the model never sees."""

    floor: float = float(MAX_CONSEQUENCE)
    max_items: int = Field(default=10, ge=0)
    preference_cap: int = Field(default=3, ge=0)
    rationale_mode: Literal["none", "minimal"] = "minimal"
    adjustments: Adjustments = Adjustments()

    @model_validator(mode="after")
    def _check_floor_matches_the_consequence_scale(self) -> PolicySettings:
        """The floor is an invariant wearing a tunable's clothes.

        It equals the maximum consequence score so that an article of maximum
        consequence matching none of the reader's topics still qualifies. Raise
        it and off-topic items become unreachable — the system quietly decays
        into a topic filter, with no error anywhere. See `docs/scoring-spec.md`.
        """
        if self.floor != MAX_CONSEQUENCE:
            raise ValueError(
                f"floor is {self.floor} but must equal the maximum consequence "
                f"score ({MAX_CONSEQUENCE}); see docs/scoring-spec.md §3"
            )
        return self


class PublishSettings(Section):
    """The public surface. Deliberately bland: no name, employer or project."""

    window: int = Field(default=100, ge=1)
    title: str = "Curated feed"
    author: str = "curated-feed"
    site_url: str = ""
    path_prefix: str = ""


class DeploySettings(Section):
    """Where the built feed is uploaded, and by what.

    No credential belongs here. The deploy tool resolves its own from the
    environment, exactly as the scorer does, so the repository never holds one.
    """

    provider: str = "none"
    project: str = ""
    compatibility_date: str = Field(default="2026-08-14", pattern=r"\d{4}-\d{2}-\d{2}")
    timeout_seconds: float = Field(default=300.0, gt=0)


class Config(Section):
    """Resolved settings for a run."""

    paths: Paths
    fetch: FetchSettings = FetchSettings()
    paywall: PaywallSettings = PaywallSettings()
    score: ScoreSettings = ScoreSettings()
    policy: PolicySettings = PolicySettings()
    publish: PublishSettings = PublishSettings()
    deploy: DeploySettings = DeploySettings()

    def with_overrides(self, **sections: dict[str, Any] | None) -> Config:
        """Return a copy with any non-None override applied, section by section.

        Overrides arrive from command-line flags, where "not given" is `None`
        and must leave the configured value alone.
        """
        data = self.model_dump()
        for name, values in sections.items():
            if name not in data:
                raise ConfigError(f"unknown config section: {name}")
            given = {
                key: value for key, value in (values or {}).items() if value is not None
            }
            data[name].update(given)
        try:
            return Config.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"invalid override: {_first_error(exc)}") from exc


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def find_config_file(start: Path | None = None) -> Path:
    """Locate the config file, searching `start` and its parents."""
    directory = (start or Path.cwd()).resolve()
    for candidate_dir in [directory, *directory.parents]:
        candidate = candidate_dir / DEFAULT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no {DEFAULT_CONFIG_FILENAME} found in {directory} or any parent directory; "
        "pass --config with an explicit path"
    )


def load_config(config_path: Path | None = None) -> Config:
    """Load settings from TOML. Paths are resolved relative to the config file."""
    path = Path(config_path).resolve() if config_path else find_config_file()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    raw["paths"] = _resolve_paths(raw.get("paths", {}), root=path.parent)
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_first_error(exc)}") from exc


def _resolve_paths(paths: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Make every path absolute, so commands work from any directory."""
    resolved = {}
    for key, value in paths.items():
        candidate = Path(str(value)).expanduser()
        resolved[key] = (
            candidate if candidate.is_absolute() else (root / candidate).resolve()
        )
    return resolved


def _first_error(exc: ValidationError) -> str:
    """The first validation problem, phrased for someone editing a TOML file.

    >>> from pydantic import BaseModel
    >>> class Example(BaseModel):
    ...     count: int
    >>> try:
    ...     Example.model_validate({})
    ... except ValidationError as error:
    ...     print(_first_error(error))
    missing required setting [count]
    """
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    if error["type"] == "missing":
        return f"missing required setting [{location}]"
    return f"[{location}] {error['msg']}"

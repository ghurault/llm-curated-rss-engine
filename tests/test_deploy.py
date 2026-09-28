"""The deploy stage, exercised without ever running a command.

The upload is a subprocess, so the runner is injected: what is asserted here is
the command that would have run, and what happens to the recorded hash when it
does not.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from curated_feed.config import Config, DeploySettings, Paths
from curated_feed.deploy import (
    DEFAULT_WRANGLER_PACKAGE,
    DEPLOY_META,
    WRANGLER_PACKAGE_ENV,
    DeployError,
    NullDeployer,
    WranglerDeployer,
    build_command,
    deployed_hash,
    main,
    make_deployer,
    rendered_hash,
)
from curated_feed.models import DeployMeta, FeedMeta
from curated_feed.render import FEED_META
from curated_feed.state import StateError, write_artifact

NOW = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)

# One exact release, as an image that baked one in would name it.
PINNED_RELEASE = "wrangler@4.127.0"

DIGEST = "0123456789abcdef"
OTHER_DIGEST = "fedcba9876543210"
SETTINGS = DeploySettings(
    provider="wrangler", project="k3n8qv2t", compatibility_date="2026-08-14"
)
NO_DEPLOYER = DeploySettings()

# Comfortably more lines than a failure report keeps, so that trimming is
# actually exercised rather than merely permitted.
NOISY_LINES = 60

# What each of the two "nothing happened" paths prints, which is the only way to
# tell a hash-gated skip from a deployer that ran and had nowhere to send.
SKIPPED = "Unchanged since the last upload"
NO_HOST = "no host is configured"

CONFIG = """
[paths]
sources = "sources.opml"
corpus_dir = "corpus"
policy = "editorial-policy.md"
state_dir = "state"
build_dir = "build"

[deploy]
provider = "{provider}"
project = "k3n8qv2t"
"""


class FakeRunner:
    """Records the command and returns a canned result."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.result = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )
        self.commands: list[Sequence[str]] = []

    def __call__(self, command: Sequence[str], *, timeout: float):
        self.commands.append(command)
        return self.result


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A rendered build waiting to be uploaded."""
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "robots.txt").write_text("", encoding="utf-8")
    write_artifact(
        tmp_path / "state" / FEED_META, FeedMeta(content_hash=DIGEST, updated=NOW)
    )
    return tmp_path


def run(workspace: Path, *extra: str, provider: str = "none") -> int:
    config = workspace / "config.toml"
    config.write_text(CONFIG.format(provider=provider), encoding="utf-8")
    return main(["--config", str(config), *extra])


class TestCommand:
    def test_the_worker_name_comes_from_configuration(self):
        command = build_command(Path("/build"), settings=SETTINGS)

        assert command[command.index("--name") + 1] == "k3n8qv2t"

    def test_the_directory_is_the_one_given(self):
        command = build_command(Path("/build"), settings=SETTINGS)

        assert command[command.index("--assets") + 1] == str(Path("/build"))

    def test_a_compatibility_date_is_always_passed(self):
        """Wrangler asks for one interactively when it is missing, which in CI hangs."""
        command = build_command(Path("/b"), settings=SETTINGS)

        assert command[command.index("--compatibility-date") + 1] == "2026-08-14"

    def test_the_release_defaults_to_a_pinned_major_version(self, monkeypatch):
        monkeypatch.delenv(WRANGLER_PACKAGE_ENV, raising=False)
        command = build_command(Path("/b"), settings=SETTINGS)

        assert command[command.index("--yes") + 1] == DEFAULT_WRANGLER_PACKAGE

    def test_the_environment_names_the_release(self, monkeypatch):
        """The image pins the release it baked in, so engine code does not."""
        monkeypatch.setenv(WRANGLER_PACKAGE_ENV, "wrangler")
        command = build_command(Path("/b"), settings=SETTINGS)

        assert command[command.index("--yes") + 1] == "wrangler"

    def test_an_empty_variable_means_the_default(self, monkeypatch):
        """As an empty [score] effort means "omit", not "send nothing"."""
        monkeypatch.setenv(WRANGLER_PACKAGE_ENV, "")
        command = build_command(Path("/b"), settings=SETTINGS)

        assert command[command.index("--yes") + 1] == DEFAULT_WRANGLER_PACKAGE


class TestDeployer:
    def test_a_dry_run_reports_the_command_and_runs_nothing(self):
        runner = FakeRunner()
        deployer = WranglerDeployer(SETTINGS, dry_run=True, runner=runner)

        report = deployer.deploy(Path("/build"))

        assert not deployer.is_live
        assert runner.commands == []
        assert "wrangler" in report

    def test_a_successful_upload_returns_what_the_tool_printed(self):
        runner = FakeRunner(stdout="Deployment complete.\n")
        deployer = WranglerDeployer(SETTINGS, runner=runner)

        assert deployer.deploy(Path("/build")) == "Deployment complete."
        assert len(runner.commands) == 1

    def test_a_failed_upload_names_the_release_that_ran(self, monkeypatch):
        """Which wrangler ran is the first question a failure raises."""
        monkeypatch.setenv(WRANGLER_PACKAGE_ENV, PINNED_RELEASE)
        runner = FakeRunner(returncode=1, stderr="Authentication error\n")
        deployer = WranglerDeployer(SETTINGS, runner=runner)

        with pytest.raises(DeployError, match=re.escape(PINNED_RELEASE)):
            deployer.deploy(Path("/build"))

    def test_a_failed_upload_reports_the_reason(self):
        runner = FakeRunner(returncode=1, stderr="Uploading...\nAuthentication error\n")
        deployer = WranglerDeployer(SETTINGS, runner=runner)

        with pytest.raises(DeployError, match="Authentication error"):
            deployer.deploy(Path("/build"))

    def test_the_reason_survives_a_sign_off_printed_after_it(self):
        """Wrangler ends with the path to its log file, which is not the reason."""
        runner = FakeRunner(
            returncode=1,
            stderr=(
                "[ERROR] A request to the Cloudflare API failed.\n"
                "\n"
                "Logs were written to /home/runner/.config/.wrangler/logs/w.log\n"
            ),
        )
        deployer = WranglerDeployer(SETTINGS, runner=runner)

        with pytest.raises(DeployError, match="A request to the Cloudflare API failed"):
            deployer.deploy(Path("/build"))

    def test_a_noisy_failure_is_trimmed_to_its_tail(self):
        """The reason is near the end; the banner and progress above it are not."""
        noise = "\n".join(f"step {number}" for number in range(NOISY_LINES))
        runner = FakeRunner(returncode=1, stderr=noise)
        deployer = WranglerDeployer(SETTINGS, runner=runner)

        with pytest.raises(DeployError) as failure:
            deployer.deploy(Path("/build"))

        assert f"step {NOISY_LINES - 1}" in str(failure.value)
        assert "step 0\n" not in str(failure.value)

    def test_a_missing_toolchain_says_where_to_deploy_from(self):
        def missing(command: Sequence[str], *, timeout: float):
            raise FileNotFoundError(command[0])

        deployer = WranglerDeployer(SETTINGS, runner=missing)

        with pytest.raises(DeployError, match="Node toolchain"):
            deployer.deploy(Path("/build"))

    def test_a_hanging_upload_is_not_waited_on_forever(self):
        def hang(command: Sequence[str], *, timeout: float):
            raise subprocess.TimeoutExpired(list(command), timeout)

        deployer = WranglerDeployer(SETTINGS, runner=hang)

        with pytest.raises(DeployError, match="timed out"):
            deployer.deploy(Path("/build"))

    def test_the_default_deployer_publishes_nothing(self, tmp_path: Path):
        deployer = make_deployer(_config(tmp_path))

        assert isinstance(deployer, NullDeployer)
        assert not deployer.is_live

    def test_an_unnamed_project_is_refused(self, tmp_path: Path):
        config = _config(tmp_path, deploy=DeploySettings(provider="wrangler"))

        with pytest.raises(DeployError, match="project"):
            make_deployer(config)

    def test_an_unknown_provider_is_refused(self, tmp_path: Path):
        config = _config(tmp_path, deploy=DeploySettings(provider="ftp"))

        with pytest.raises(DeployError, match="unknown deploy provider"):
            make_deployer(config)


class TestPublishedHash:
    def test_an_unrendered_feed_cannot_be_deployed(self, tmp_path: Path):
        with pytest.raises(StateError, match="curate-render"):
            rendered_hash(tmp_path / FEED_META)

    def test_nothing_deployed_yet_reads_as_no_hash(self, tmp_path: Path):
        assert deployed_hash(tmp_path / DEPLOY_META) is None

    def test_the_recorded_hash_is_read_back(self, tmp_path: Path):
        path = tmp_path / DEPLOY_META
        write_artifact(path, DeployMeta(content_hash=DIGEST, deployed_at=NOW))

        assert deployed_hash(path) == DIGEST


class TestCommandLine:
    def test_an_unchanged_feed_is_not_uploaded_again(self, workspace: Path, capsys):
        """A quiet day should cost nothing, and leave the host's history alone."""
        _record_deployment(workspace, DIGEST)

        assert run(workspace) == 0
        assert SKIPPED in capsys.readouterr().out

    def test_a_changed_feed_reaches_the_deployer(self, workspace: Path, capsys):
        _record_deployment(workspace, OTHER_DIGEST)

        assert run(workspace) == 0
        assert NO_HOST in capsys.readouterr().out

    def test_a_feed_never_deployed_reaches_the_deployer(self, workspace: Path, capsys):
        assert run(workspace) == 0
        assert NO_HOST in capsys.readouterr().out

    def test_force_overrides_the_hash_gate(self, workspace: Path, capsys):
        _record_deployment(workspace, DIGEST)

        assert run(workspace, "--force") == 0
        assert NO_HOST in capsys.readouterr().out

    def test_a_dry_run_records_nothing(self, workspace: Path):
        """Recording it would make the next run skip an upload that never happened."""
        assert run(workspace, "--dry-run", provider="wrangler") == 0
        assert not (workspace / "state" / DEPLOY_META).exists()

    def test_an_unrendered_feed_is_a_configuration_error(self, tmp_path: Path):
        assert run(tmp_path) != 0

    def test_an_unknown_provider_is_reported(self, workspace: Path):
        assert run(workspace, "--provider", "ftp") != 0


def _record_deployment(workspace: Path, digest: str) -> None:
    write_artifact(
        workspace / "state" / DEPLOY_META,
        DeployMeta(content_hash=digest, deployed_at=NOW),
    )


def _config(tmp_path: Path, *, deploy: DeploySettings = NO_DEPLOYER) -> Config:
    return Config(
        paths=Paths(
            sources=tmp_path / "s.opml",
            corpus_dir=tmp_path / "corpus",
            policy=tmp_path / "policy.md",
            state_dir=tmp_path / "state",
            build_dir=tmp_path / "build",
        ),
        deploy=deploy,
    )

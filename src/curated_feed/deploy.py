"""Putting the built feed in front of the internet.

Rendering stops at the filesystem, because the build host and the publish host
are separate (see `docs/ARCHITECTURE.md`, D6). This stage is the only one that knows
where the feed is served from, and the only one whose failure is invisible: a
reader that stops receiving items looks exactly like a quiet week.

Three things follow from that.

- **Uploading is gated on the content hash.** Rendering records the hash of what
  it wrote; this records the hash of what it sent. A quiet day therefore costs
  nothing, and the host's deployment history stays a list of real changes rather
  than a daily heartbeat.
- **Nothing is written unless an upload actually happened.** A deployer that
  reports what it would do must not be recorded as having done it, or the next
  run would skip an upload that never took place.
- **No credential is read from configuration.** The deploy tool resolves its
  own from the environment, exactly as the scorer does.

Deployment belongs to CI. The devcontainer can rehearse it, and does not have to
be trusted with a token to be useful.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .config import Config, ConfigError, DeploySettings, load_config
from .models import DeployMeta, FeedMeta
from .render import FEED_META
from .state import StateError, read_artifact, write_artifact

DEPLOY_META = "deploy.json"

# Which wrangler to run, and where the answer comes from.
#
# A bare `npx wrangler` would fetch whatever is newest at six in the morning,
# which is a moving part in the one stage whose failure nobody sees until a
# reader stops updating. Pinning it here instead would put the version in engine
# code, where the image that has to carry a matching Node toolchain cannot reach
# it — so the pin belongs to whatever supplies the toolchain, and the variable is
# how it says so. The image installs one release and names it here; the pin then
# travels with the image digest, which is already how a run chooses an engine.
#
# The default keeps a checkout working with no toolchain of its own, at the cost
# of the registry lookup that resolves the range.
WRANGLER_PACKAGE_ENV = "CURATED_FEED_WRANGLER"
DEFAULT_WRANGLER_PACKAGE = "wrangler@4"

# How much of a failed upload's output to repeat. Enough to carry the reason
# along with the sign-off that follows it, and few enough that a progress log
# does not bury it.
ERROR_LINES = 10


def wrangler_package(env: Mapping[str, str] | None = None) -> str:
    """The wrangler `npx` is asked for.

    An empty variable means the default rather than an empty argument, as an
    empty `[score] effort` means "leave it out" rather than "send nothing".

    A bare package name is npx's "run what is already installed" form, which is
    what the image sets it to: nothing is resolved and nothing is downloaded.

    >>> wrangler_package({})
    'wrangler@4'
    >>> wrangler_package({"CURATED_FEED_WRANGLER": "wrangler"})
    'wrangler'
    """
    if env is None:
        env = os.environ
    return env.get(WRANGLER_PACKAGE_ENV, "").strip() or DEFAULT_WRANGLER_PACKAGE


class DeployError(Exception):
    """Raised when the built feed could not be uploaded."""


class Deployer(Protocol):
    """Anything that can publish a directory of files.

    `is_live` says whether `deploy` really publishes. It is part of the protocol
    rather than a detail of one backend, because the caller decides on it
    whether to record the upload.
    """

    name: str
    is_live: bool

    def deploy(self, directory: Path) -> str: ...


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


class NullDeployer:
    """Explains that no host is configured, and uploads nothing.

    The default, and deliberately so: a replay of a stub day is the commonest
    thing to run locally, and it must not be able to reach the internet.
    """

    name = "none"
    is_live = False

    def deploy(self, directory: Path) -> str:
        return (
            f"{directory} was not uploaded: no host is configured. "
            "Set [deploy] provider to publish it."
        )


CompletedCommand = subprocess.CompletedProcess[str]
Runner = Callable[..., CompletedCommand]


def run_command(command: Sequence[str], *, timeout: float) -> CompletedCommand:
    """Run a command to completion, capturing what it printed."""
    return subprocess.run(  # noqa: S603 - a fixed argument list, and no shell
        command, capture_output=True, text=True, timeout=timeout, check=False
    )


def build_command(
    directory: Path, *, settings: DeploySettings, package: str | None = None
) -> list[str]:
    """The wrangler invocation for one upload.

    Every value wrangler needs is passed as a flag, so that no `wrangler.jsonc`
    has to be committed. That file would hold the Worker's name, which is the
    hostname the feed is served from and therefore the secret.

    Without a config file wrangler asks for a compatibility date, and asks
    interactively when it is missing — which in CI is a hang rather than a
    question. It has no effect on a Worker that is only static files.

    >>> settings = DeploySettings(project="p", compatibility_date="2026-08-14")
    >>> build_command(Path("/build"), settings=settings)[3:]
    ['deploy', '--assets', '/build', '--name', 'p', '--compatibility-date', '2026-08-14']
    """
    return [
        "npx",
        "--yes",
        package or wrangler_package(),
        "deploy",
        "--assets",
        str(directory),
        "--name",
        settings.project,
        "--compatibility-date",
        settings.compatibility_date,
    ]


class WranglerDeployer:
    """Uploads a directory to a Cloudflare Worker with `wrangler deploy`.

    Wrangler resolves `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` from
    the environment, so neither appears in configuration.

    A Worker serving static assets, rather than Cloudflare Pages: the two put
    the same files on the same edge network, but a Pages deployment additionally
    mints a permanent `<hash>.<project>.pages.dev` address every time it runs.
    A daily job would therefore accumulate public addresses for a feed whose
    privacy is its URL. `wrangler deploy` updates one address in place.
    """

    name = "wrangler"

    def __init__(
        self,
        settings: DeploySettings,
        *,
        dry_run: bool = False,
        runner: Runner = run_command,
    ) -> None:
        self.settings = settings
        self.is_live = not dry_run
        self._run = runner

    def deploy(self, directory: Path) -> str:
        package = wrangler_package()
        command = build_command(directory, settings=self.settings, package=package)
        if not self.is_live:
            return f"would run: {shlex.join(command)}"

        try:
            result = self._run(command, timeout=self.settings.timeout_seconds)
        except FileNotFoundError as exc:
            raise DeployError(
                f"{command[0]} was not found. Uploading needs a Node toolchain, "
                "which the image carries and a bare virtual environment does "
                "not: deploy from CI, or from the devcontainer."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DeployError(
                f"upload timed out after {self.settings.timeout_seconds:g}s"
            ) from exc

        if result.returncode != 0:
            raise DeployError(
                f"{package} exited {result.returncode}:\n"
                f"{_tail(result.stderr) or _tail(result.stdout)}"
            )
        return result.stdout.strip() or "uploaded"


def _tail(output: str, limit: int = ERROR_LINES) -> str:
    r"""The last few non-blank lines of a command's output.

    A failing wrangler run prints a banner, then progress, then the reason, and
    then signs off with the path to its log file. The reason is therefore
    neither the first line nor the last, and reporting a single line reports the
    sign-off — which in CI, where that log file is thrown away with the runner,
    says nothing at all.

    >>> _tail("Uploading...\nAuth error\n\nLogs were written to /tmp/w.log\n", limit=2)
    'Auth error\nLogs were written to /tmp/w.log'
    >>> _tail("")
    ''
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def make_deployer(config: Config, *, dry_run: bool = False) -> Deployer:
    """The deployer named in configuration."""
    provider = config.deploy.provider
    if provider == "none":
        return NullDeployer()
    if provider == "wrangler":
        if not config.deploy.project:
            raise DeployError("the wrangler deployer needs [deploy] project to be set")
        return WranglerDeployer(config.deploy, dry_run=dry_run)
    raise DeployError(f"unknown deploy provider: {provider!r} (known: none, wrangler)")


# --------------------------------------------------------------------------- #
# What is already published
# --------------------------------------------------------------------------- #


def rendered_hash(path: Path) -> str:
    """The content hash of the build on disk, as the last render recorded it."""
    if not path.exists():
        raise StateError(
            f"nothing to deploy: {path} does not exist; run curate-render first"
        )
    return read_artifact(path, FeedMeta).content_hash


def deployed_hash(path: Path) -> str | None:
    """The content hash of the last successful upload, or `None` if there is none."""
    if not path.exists():
        return None
    return read_artifact(path, DeployMeta).content_hash


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-deploy",
        description="Upload the built feed to the host it is served from.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--provider", help="deployer to use, overriding the config")
    parser.add_argument("--build-dir", type=Path, help="directory to upload")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the upload that would run, and upload nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="upload even when the feed has not changed since the last one",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config).with_overrides(
            paths={"build_dir": args.build_dir},
            deploy={"provider": args.provider},
        )
        deployer = make_deployer(config, dry_run=args.dry_run)
        meta_path = config.paths.state_dir / DEPLOY_META
        digest = rendered_hash(config.paths.state_dir / FEED_META)
        is_current = digest == deployed_hash(meta_path)
    except (ConfigError, StateError, DeployError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    directory = config.paths.build_dir
    if is_current and not args.force:
        print(f"\nUnchanged since the last upload; {directory} was not sent.")
        return 0
    if not directory.is_dir():
        print(f"error: nothing to deploy: {directory} does not exist", file=sys.stderr)
        return 2

    print(f"Deploying {directory} with the {deployer.name} deployer")
    try:
        report = deployer.deploy(directory)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if deployer.is_live:
        write_artifact(
            meta_path,
            DeployMeta(content_hash=digest, deployed_at=datetime.now(UTC)),
        )
    print(f"\n{report}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

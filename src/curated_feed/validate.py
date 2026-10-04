"""Preflight checks on a runner's configuration and the files it names.

The settings model refuses what is provably wrong — an unknown key, a floor that
does not match the consequence scale — and refusing is the right answer there,
because a config that cannot load is a config nobody can run against. This
module is for what is *probably* wrong: an editorial policy that was copied but
never written, a subscription list that lists nothing, two runners that name one
state directory. None of those stops a run. Each of them ruins one, silently, at
six in the morning.

The line the two sit either side of is who pays for a false positive. A model
validator that guesses wrong makes the config unloadable and the only escape is
patching the engine; a check here that guesses wrong is a line to argue with.

Nothing here touches the network, resolves a feed or needs a credential, which
is what lets the same command serve as a pull-request gate, an unattended
preflight inside a container that holds no secrets, and something run by hand
after copying a runner's directory.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from .config import (
    DEFAULT_CONFIG_FILENAME,
    Config,
    ConfigError,
    DeploySettings,
    Paths,
    PublishSettings,
    load_config,
)
from .opml import OpmlError, parse_opml
from .render import FALLBACK_FEED_ID, feed_url

# Cloudflare serves a Worker at `<worker>.<subdomain>.workers.dev` unless it is
# given a custom domain, which is what makes the Worker's name readable off the
# host — and only there.
WORKERS_DEV = ".workers.dev"

# What no two runners may name, and how to read it off a config. Sharing any of
# them means two runners appending to one published log, or racing for one
# Worker — neither of which reports anything on its own.
RESOURCE_READERS: dict[str, Callable[[Config], object]] = {
    "build directory": lambda config: config.paths.build_dir,
    "corpus directory": lambda config: config.paths.corpus_dir,
    "feed URL": lambda config: feed_url(config.publish),
    "state directory": lambda config: config.paths.state_dir,
    "Worker": lambda config: config.deploy.project,
}

# What a resource reads as when the runner has not named one. Two runners that
# name no Worker are not fighting over one, and two that publish nowhere render
# the same placeholder id without either of them serving it.
UNSET_RESOURCES = frozenset({"", FALLBACK_FEED_ID})


# %%
# What a runner's own files must be


def check_the_editorial_policy_is_written(paths: Paths) -> str | None:
    """The runner's half of the prompt, which the engine ships the other half of.

    Present but empty is the state a copied template is left in, and it reaches
    the model as a heading and nothing else: every article scores on consequence
    alone, and the day looks merely disappointing rather than broken.
    """
    try:
        text = paths.policy.read_text(encoding="utf-8")
    except OSError:
        return f"no editorial policy at {paths.policy}"
    except UnicodeDecodeError:
        return f"the editorial policy at {paths.policy} is not text"
    if not text.strip():
        return f"the editorial policy at {paths.policy} is empty"
    return None


def check_the_subscription_list_holds_feeds(paths: Paths) -> str | None:
    """The runner's subscriptions, parsed rather than resolved.

    Reading the file is filesystem work; asking each feed whether it answers is
    the network call this command may not make. A list that is present but lists
    nothing fails at fetch instead, unattended, with a run summary reporting
    zero items and no reason for it.
    """
    try:
        feeds = parse_opml(paths.sources)
    except OpmlError as exc:
        return str(exc)
    if not feeds:
        return (
            f"the subscription list at {paths.sources} lists no feeds; "
            "export one from your reader and commit it"
        )
    return None


# %%
# What the feed claims about itself, against where it is uploaded


def check_the_feed_has_an_id(
    publish: PublishSettings, deploy: DeploySettings
) -> str | None:
    """A feed that is uploaded needs an address to call its own.

    `feed_url` falls back to a fixed urn when `site_url` is empty, so the
    published document's id and self-link are a placeholder shared with every
    other runner in that state — and a reader keys on the id.

    >>> print(check_the_feed_has_an_id(
    ...     PublishSettings(site_url=""), DeploySettings(provider="wrangler")))
    [deploy] provider is 'wrangler' but [publish] site_url is empty, so the feed's id is the placeholder 'urn:curated-feed'
    >>> check_the_feed_has_an_id(PublishSettings(site_url=""), DeploySettings())
    """
    if deploy.provider == "none" or publish.site_url:
        return None
    return (
        f"[deploy] provider is {deploy.provider!r} but [publish] site_url is "
        f"empty, so the feed's id is the placeholder {FALLBACK_FEED_ID!r}"
    )


def check_the_site_url_is_absolute(publish: PublishSettings) -> str | None:
    """The feed's own address, which is published verbatim.

    It becomes the id and the self-link of every document rendered, so a value
    a reader cannot resolve is not caught by anything downstream.

    >>> print(check_the_site_url_is_absolute(PublishSettings(site_url="a.example")))
    [publish] site_url 'a.example' is not an absolute URL; it needs a scheme and a host
    >>> check_the_site_url_is_absolute(PublishSettings(site_url="https://a.example"))
    """
    if not publish.site_url or urlsplit(publish.site_url).hostname:
        return None
    return (
        f"[publish] site_url {publish.site_url!r} is not an absolute URL; "
        "it needs a scheme and a host"
    )


def check_the_worker_matches_the_host(
    publish: PublishSettings, deploy: DeploySettings
) -> str | None:
    """Where the bytes go, against what the bytes claim about themselves.

    `deploy.project` becomes `wrangler deploy --name <project>` and decides the
    first; `publish.site_url` feeds `feed_url` and decides the second. Nothing
    ties them together but a comment in each config, and the edit where they
    drift is rotating the random name after a leak — which is exactly when a
    feed served at one address and advertising another matters most.

    Only inferable on a `workers.dev` host: the convention is Cloudflare's
    rather than a rule, and a custom domain breaks it entirely. Being an
    inference is also why it lives here and not in a model validator — a wrong
    guess there makes the config unloadable and stops the 06:00 run outright.

    >>> print(check_the_worker_matches_the_host(
    ...     PublishSettings(site_url="https://frog-a8b1.someone.workers.dev"),
    ...     DeploySettings(provider="wrangler", project="frog-a8b2")))
    [publish] site_url is served by Worker 'frog-a8b1', but [deploy] project is 'frog-a8b2'
    >>> check_the_worker_matches_the_host(
    ...     PublishSettings(site_url="https://feed.example.com"),
    ...     DeploySettings(provider="wrangler", project="frog-a8b2"))
    """
    if deploy.provider != "wrangler":
        return None
    host = urlsplit(publish.site_url).hostname or ""
    if not host.endswith(WORKERS_DEV):
        return None
    worker = host.split(".")[0]
    if worker == deploy.project.lower():
        return None
    return (
        f"[publish] site_url is served by Worker {worker!r}, "
        f"but [deploy] project is {deploy.project!r}"
    )


# %%
# One runner


RUNNER_CHECKS: tuple[Callable[[Config], str | None], ...] = (
    lambda config: check_the_editorial_policy_is_written(config.paths),
    lambda config: check_the_subscription_list_holds_feeds(config.paths),
    lambda config: check_the_feed_has_an_id(config.publish, config.deploy),
    lambda config: check_the_site_url_is_absolute(config.publish),
    lambda config: check_the_worker_matches_the_host(config.publish, config.deploy),
)


def validate_config(config: Config) -> list[str]:
    """Every problem in one runner's settings and the files they name.

    What the runner writes to — its corpus, state and build directories — is
    deliberately not required to exist. A fresh checkout has none of them, and
    neither does the container this runs in as a preflight.
    """
    problems = (check(config) for check in RUNNER_CHECKS)
    return [problem for problem in problems if problem is not None]


def validate_runner(config_path: Path) -> list[str]:
    """Every problem in the runner whose config is at `config_path`.

    The directory names the runner, as it does everywhere else (`ARCHITECTURE.md`
    D8), so that is what each line is prefixed with.
    """
    name = config_path.parent.name
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return [f"{name}: {exc}"]
    return [f"{name}: {problem}" for problem in validate_config(config)]


# %%
# A directory of runners


def find_runners(root: Path) -> list[Path]:
    """The runner directories under `root`, in name order.

    A runner is a subdirectory holding a config, which is the same rule the
    workflow's matrix is held to. Anything else under `root` is reported rather
    than curated, so that a directory copied but not yet given a config says so.
    """
    return sorted(
        path for path in root.iterdir() if (path / DEFAULT_CONFIG_FILENAME).is_file()
    )


def check_runners_share_nothing(config_by_runner: dict[str, Config]) -> list[str]:
    """No two runners may name one resource.

    Nothing derives these locations from the runner's name, so a config that was
    copied and half-edited would publish two feeds into one log and upload each
    over the other. Both feeds still render, which is why this is checked rather
    than left to be noticed.
    """
    problems = []
    for resource, read in sorted(RESOURCE_READERS.items()):
        owner_by_value: dict[object, str] = {}
        for name, config in sorted(config_by_runner.items()):
            value = read(config)
            if value in UNSET_RESOURCES:
                continue
            owner = owner_by_value.setdefault(value, name)
            if owner != name:
                problems.append(f"{name}: {resource} {value!r} is also {owner}'s")
    return problems


def validate_directory(root: Path) -> list[str]:
    """Every problem in every runner under `root`, and between them."""
    problems = []

    stray = root / DEFAULT_CONFIG_FILENAME
    if stray.is_file():
        # `find_config_file` searches parent directories, so this one is the
        # silent fallback for any command run from inside a runner-less
        # subdirectory of `root` — a run with somebody else's paths.
        problems.append(f"{stray} is not a runner's config, and would be found as one")

    runners = find_runners(root)
    known = set(runners)
    problems += [
        f"{path.name}: no {DEFAULT_CONFIG_FILENAME} in {path}"
        for path in sorted(root.iterdir())
        if path.is_dir() and path not in known
    ]
    if not runners:
        problems.append(f"no runner under {root}: no subdirectory holds a config")
        return problems

    config_by_runner: dict[str, Config] = {}
    for path in runners:
        try:
            config = load_config(path / DEFAULT_CONFIG_FILENAME)
        except ConfigError as exc:
            # Reported rather than raised, so that one unloadable config does
            # not hide every problem in every other runner.
            problems.append(f"{path.name}: {exc}")
            continue
        config_by_runner[path.name] = config
        problems += [f"{path.name}: {problem}" for problem in validate_config(config)]

    return problems + check_runners_share_nothing(config_by_runner)


def validate_path(path: Path) -> list[str]:
    """Every problem under one config file, or under one directory of runners."""
    if path.is_dir():
        return validate_directory(path)
    return validate_runner(path)


# %%
# Command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-validate",
        description=(
            "Check a runner's configuration and the files it names. "
            "Reads nothing but the filesystem: no feed is resolved and no "
            "credential is needed. Exits non-zero with one line per problem."
        ),
    )
    # Required, and not found by searching upwards the way every other command's
    # --config is. A repository with no config at its root is deliberate, and a
    # discovering validator would resurrect the "there is one feed" trap under a
    # new name — silently validating a runner nobody asked about.
    parser.add_argument(
        "path",
        type=Path,
        help=(
            "a runner's config.toml, or a directory whose subdirectories are "
            "runners (which also checks that they share nothing)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path: Path = args.path

    if not path.exists():
        print(f"error: not found: {path}", file=sys.stderr)
        return 2

    problems = validate_path(path)
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s) in {path}")
        return 1

    print(f"{path}: nothing to report")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

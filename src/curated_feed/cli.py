"""One command that runs the daily job end to end.

Each stage is invoked exactly as it would be on its own, so the chain reads and
writes the same artifacts a replay does. Nothing here knows how a stage works;
going through the files is the design rather than an inefficiency, and it means
this module cannot drift away from the commands it calls.

`--date` selects a past collection day, which is also what replay means: a day
already fetched can be re-scored, re-selected and re-rendered as often as a
policy is edited, so tuning is a loop of seconds. Fetching is therefore skipped
whenever a date is given — yesterday's feeds no longer hold yesterday's news.

Publishing is the one stage that is opt-in. Replaying a saved day is the
commonest thing to run by hand, and it is usually run against the stub scorer;
having that reach the internet by default would be a footgun with no upside,
since the only run that should publish is the scheduled one.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from . import deploy, fetch, paywall, render, score, select

STAGES = ("fetch", "score", "paywall", "select", "render", "deploy")
DATED_STAGES = frozenset({"score", "paywall", "select", "render"})
PUBLISHING_STAGE = "deploy"

MAIN_BY_STAGE: dict[str, Callable[[list[str]], int]] = {
    "fetch": fetch.main,
    "score": score.main,
    "paywall": paywall.main,
    "select": select.main,
    "render": render.main,
    "deploy": deploy.main,
}


def first_stage(args: argparse.Namespace) -> str:
    """Where to start: what was asked for, or what the arguments imply.

    >>> first_stage(argparse.Namespace(from_stage=None, date=None))
    'fetch'
    >>> first_stage(argparse.Namespace(from_stage=None, date=date(2026, 8, 8)))
    'score'
    """
    if args.from_stage:
        return args.from_stage
    return "score" if args.date else "fetch"


def last_stage(args: argparse.Namespace) -> str:
    """Where to stop: rendering, unless publishing was asked for.

    Starting *at* the publishing stage is asking for it too, since there is
    nothing else left to do.

    >>> last_stage(argparse.Namespace(deploy=False, from_stage=None))
    'render'
    >>> last_stage(argparse.Namespace(deploy=True, from_stage=None))
    'deploy'
    >>> last_stage(argparse.Namespace(deploy=False, from_stage='deploy'))
    'deploy'
    """
    if args.deploy or args.from_stage == PUBLISHING_STAGE:
        return PUBLISHING_STAGE
    return "render"


def stage_argv(stage: str, args: argparse.Namespace) -> list[str]:
    """The arguments this stage would have been given on its own."""
    argv: list[str] = []
    if args.config:
        argv += ["--config", str(args.config)]
    if args.date and stage in DATED_STAGES:
        argv += ["--date", args.date.isoformat()]
    if stage == "score":
        if args.provider:
            argv += ["--provider", args.provider]
        if args.response:
            argv += ["--response", str(args.response)]
        if args.scoring_prompt:
            argv += ["--scoring-prompt", str(args.scoring_prompt)]
    return argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-run",
        description="Fetch, score, select and render, in that order.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="replay a past collection day, starting at scoring",
    )
    parser.add_argument(
        "--from",
        dest="from_stage",
        choices=STAGES,
        help="start at this stage instead of the beginning",
    )
    parser.add_argument(
        "--provider",
        help="scorer to use (stub, file or anthropic), overriding the config",
    )
    parser.add_argument(
        "--response",
        type=Path,
        metavar="PATH",
        help="saved response to read, for the file scorer",
    )
    parser.add_argument(
        "--scoring-prompt",
        type=Path,
        metavar="PATH",
        help="scoring prompt to use instead of the one shipped with the package",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="publish the rendered feed as well, instead of stopping at build/",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    first = STAGES.index(first_stage(args))
    last = STAGES.index(last_stage(args))

    for stage in STAGES[first : last + 1]:
        print(f"\n=== {stage} ===")
        code = MAIN_BY_STAGE[stage](stage_argv(stage, args))
        if code != 0:
            print(f"\nstopped at {stage}")
            return code
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

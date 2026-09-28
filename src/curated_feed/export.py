"""Render a corpus file as compact Markdown for pasting into a chat session.

A collection day can run to several hundred items, so the output is deliberately
terse: two lines per item, numbered for reference and carrying the record `id` so
that picks can be traced back to the corpus.

The numbering is the same one `policy.py` puts in the prompt, and comes from the
same renderer. A chat session and a pipeline run therefore refer to an article by
the same number, which is what lets a response saved from one drive the other.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import Config, ConfigError, load_config
from .models import Candidate
from .policy import build_prompt, render_candidates
from .state import StateError, latest_corpus_path, read_corpus


def render_markdown(
    candidates: Sequence[Candidate], *, title: str, max_items: int | None = None
) -> str:
    """Numbered Markdown under a heading, one item per two lines."""
    shown = candidates[:max_items] if max_items else candidates

    header = f"# {title} — {len(candidates)} items"
    if len(shown) != len(candidates):
        header += f" (showing first {len(shown)})"

    return f"{header}\n\n{render_candidates(shown, with_ids=True)}\n"


def render(
    candidates: Sequence[Candidate],
    *,
    config: Config,
    title: str,
    max_items: int | None = None,
    as_prompt: bool = False,
    prompt_path: Path | None = None,
) -> str:
    """The listing on its own, or the whole prompt the pipeline would send.

    The prompt form is what a chat session wants: the same policy documents and
    the same numbering the pipeline uses, so a response saved from that session
    can be fed straight back with `curate-score --provider file`.
    """
    if as_prompt:
        shown = candidates[:max_items] if max_items else candidates
        prompt = build_prompt(
            shown, policy_path=config.paths.policy, prompt_path=prompt_path
        )
        return prompt.as_text() + "\n"
    return render_markdown(candidates, title=title, max_items=max_items)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curate-export",
        description="Render a corpus file as compact Markdown for review.",
    )
    parser.add_argument(
        "corpus_file",
        nargs="?",
        type=Path,
        help="corpus file to render (default: the most recent one)",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--corpus-dir", type=Path, help="directory holding corpus files"
    )
    parser.add_argument("--max-items", type=int, help="render at most this many items")
    parser.add_argument("--out", type=Path, help="write to this file instead of stdout")
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="emit the whole scoring prompt, policy documents included",
    )
    parser.add_argument(
        "--scoring-prompt",
        type=Path,
        metavar="PATH",
        help="scoring prompt to use instead of the one shipped with the package",
    )
    return parser


def resolve_corpus_file(args: argparse.Namespace, config: Config) -> Path:
    if args.corpus_file:
        path = Path(args.corpus_file)
        if not path.is_file():
            raise StateError(f"corpus file not found: {path}")
        return path
    return latest_corpus_path(config.paths.corpus_dir)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config).with_overrides(
            paths={"corpus_dir": args.corpus_dir}
        )
        path = resolve_corpus_file(args, config)
        records = read_corpus(path)
        shown = records[: args.max_items] if args.max_items else records
        rendered = render(
            records,
            config=config,
            title=path.stem,
            max_items=args.max_items,
            as_prompt=args.prompt,
            prompt_path=args.scoring_prompt,
        )
    except (ConfigError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
        print(
            f"wrote {len(shown)} of {len(records)} item(s) to {args.out}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

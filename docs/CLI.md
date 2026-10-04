# Command-line reference

Every command the engine installs, in the order a day runs through them.
Each section is generated from the parser the command itself uses, so it matches `curate-<command> --help` for the installed version.
On GitHub, where the sections below do not render, read [the published page](https://ghurault.github.io/llm-curated-rss-engine/CLI.html) or run `--help` instead.

Commands are shown bare, as an installed package provides them; from the image, prefix each one with `engine` as in step 2 of [SETUP.md](SETUP.md).
A runner is chosen with `--config feeds/<name>/config.toml`.
Left out, it is found by searching upwards from the working directory, so a bare command works from inside `feeds/<name>/` but not from the repository root, which deliberately has no `config.toml`.
Command-line options override the config for that run only.

## curate-template

```{argparse}
:module: curated_feed.template
:func: build_parser
:prog: curate-template
```

## curate-validate

```{argparse}
:module: curated_feed.validate
:func: build_parser
:prog: curate-validate
```

## curate-fetch

```{argparse}
:module: curated_feed.fetch
:func: build_parser
:prog: curate-fetch
```

## curate-export

```{argparse}
:module: curated_feed.export
:func: build_parser
:prog: curate-export
```

## curate-score

```{argparse}
:module: curated_feed.score
:func: build_parser
:prog: curate-score
```

## curate-paywall

```{argparse}
:module: curated_feed.paywall
:func: build_parser
:prog: curate-paywall
```

## curate-select

```{argparse}
:module: curated_feed.select
:func: build_parser
:prog: curate-select
```

## curate-render

```{argparse}
:module: curated_feed.render
:func: build_parser
:prog: curate-render
```

## curate-deploy

```{argparse}
:module: curated_feed.deploy
:func: build_parser
:prog: curate-deploy
```

## curate-run

```{argparse}
:module: curated_feed.cli
:func: build_parser
:prog: curate-run
```

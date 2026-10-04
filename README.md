# LLM Curated RSS Feeds

[![tests](https://github.com/ghurault/llm-curated-rss-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/ghurault/llm-curated-rss-engine/actions/workflows/tests.yml)
[![docs](https://github.com/ghurault/llm-curated-rss-engine/actions/workflows/docs.yml/badge.svg)](https://ghurault.github.io/llm-curated-rss-engine/)
[![codecov](https://codecov.io/gh/ghurault/llm-curated-rss-engine/graph/badge.svg)](https://codecov.io/gh/ghurault/llm-curated-rss-engine)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> Disclaimer: this project is a result of a vibe-coding experiment.
> I designed the application, but did not implement it by hand.
> The resulting code might not be the simplest or most maintainable.

This project implement a news LLM-based recommender system to assist in the curation of RSS feeds.
Instead of letting the algorithm figure out what I want to read, the idea is to tell it directly what I want.

The application does the following:

1. Collects the day's new articles from an OPML subscription list.
2. Calls an LLM to score them against a written editorial policy.
3. Selects a handful of articles based on those scores.
4. Publishes the handful worth reading as a small Atom feed.

```text
fetch  →  score  →  select  →  render  →  deploy
```

One instance curates as many feeds as you like — each is a directory under `feeds/` with its own subscriptions, policy and published URL, run independently.

My use case is not to outsource the decision to read an article to AI, but rather for AI to identify articles that I would most be interested in.
I subscribe to the curated feed in my RSS reader **alongside** the raw subscriptions, so it is a highlights lane rather than a filter.

This repository is the engine, and holds nothing about any reader.
Your feeds live in a private configuration repository of your own, which runs the engine's published image on a schedule and chooses when to take a new release — [docs/SETUP.md](docs/SETUP.md) builds one from scratch.

## Features

- 🧮 **Built-in scoring system, reusable across feeds**: consequence (impact/importance of an article), topic preference and adjustments combine into one score, with a floor and a hard cap on how many articles are ever published in a day (see [docs/scoring-spec.md](docs/scoring-spec.md)).
- ✍️ **User-defined editorial policy**: preferences, hard exclusions, penalties and score adjustments, written by hand per feed.
- 🔁 **Duplicate detection by the LLM**: multiple articles covering the same story collapse into one entry, keeping the most substantive version.
- 🎲 **One discretionary "serendipity" pick per day**: room for the rare item that matters but doesn't fit the policy on paper.
- 🔒 **Optional paywall detection**: articles marked subscriber-only in their own schema.org markup are dropped before the cap, so the slot goes to something you can actually read. Off by default, switched on per feed; a page that can't be reached is treated as readable, never gated on a guess.
- 📚 **Multiple feeds from one instance**: each runner has its own sources, policy and published URL, scheduled independently.
- 🤖 **Supported AI providers**: Claude (API).
- ☁️ **Supported host providers**: Cloudflare Workers (free): static asset hosting, no build pipeline needed.
- ⚙️ **Automation via GitHub Actions (free)**: one scheduled job per feed, daily, through a reusable workflow your configuration repository pins to an exact engine release.
- 💸 **Change-aware deploys**: an unchanged feed isn't re-uploaded, so a quiet day costs nothing.
- 📝 **Optional selection rationale**: add a short, model-free explanation of why an article was picked to its description.

## Privacy

The design assumes **your configuration repository stays private**, because your sources and editorial policy describe what you care about.
Both are sent to the model provider on every run; Anthropic does not train on API inputs by default, and the provider can be swapped for a self-hosted one.
The published feed is public but hard to find: its URL is unguessable, and `robots.txt` and a `noindex` header keep it out of search engines.
[docs/PRIVACY.md](docs/PRIVACY.md) goes through what each part of the system can see.

## Layout

```text
├── src/curated_feed/            the package
│   ├── prompt.md                how to judge an article, shipped with the engine
│   ├── config.template.toml     the annotated settings reference, and what to copy
│   └── editorial-policy.template.md  the annotated taste reference, and what to copy
├── docs/                        hand-written documentation
│   ├── SETUP.md                 how to get it running, and keep it running
│   ├── CLI.md                   the command-line reference, generated from the parsers
│   ├── PRIVACY.md               what each part of the system can see
│   ├── ARCHITECTURE.md          why it is shaped this way, and the contracts
│   ├── scoring-spec.md          how judgements become a ranking
│   └── api/                     generated API reference (git ignored)
├── .github/workflows/
│   ├── curate.yml               one feed's day, called by a configuration repository
│   └── image.yml                builds, exercises and publishes the image
├── tests/                       never touch the network or a model
├── scripts/                     utility scripts
├── Dockerfile                   the image the pipeline runs from, and the devcontainer
├── CONTRIBUTING.md              running the checks and the conventions
├── pyproject.toml               package, dependencies and tool configuration
├── requirements.txt             pinned application dependencies (generated)
├── requirements-dev.txt         pinned application and development dependencies (generated)
└── Makefile                     common tasks
```

A configuration repository's own layout — `feeds/`, `eval/`, `state/` and the workflow that calls `curate.yml` — is step 1 of [docs/SETUP.md](docs/SETUP.md).

## Getting started

[docs/SETUP.md](docs/SETUP.md) sets up your own configuration repository and its first feed, then covers running it day to day.

## Commands

Each command's options are in the [command-line reference](docs/CLI.md), or its `--help`.

| command           | what it does                                                          |
| ----------------- | --------------------------------------------------------------------- |
| `curate-template` | print the annotated config or policy a new runner is copied from      |
| `curate-validate` | check a runner's configuration offline, without credentials           |
| `curate-fetch`    | collect the day's new articles into the runner's corpus               |
| `curate-export`   | render a day's corpus for review, or as a prompt to paste into chat   |
| `curate-score`    | judge a day's articles with the configured scorer                     |
| `curate-paywall`  | check which graded articles are paywalled, when the runner enables it |
| `curate-select`   | turn the model's judgements into the day's selection                  |
| `curate-render`   | add that selection to the rolling Atom feed under `build/`            |
| `curate-deploy`   | upload `build/` to the host                                           |
| `curate-run`      | the whole chain, or part of it for a past day                         |

## Reference

- **[docs/SETUP.md](docs/SETUP.md)** — setting up your own configuration repository and its first feed, running it day to day, and what to check when something is wrong.
- **[docs/CLI.md](docs/CLI.md)** — every command and its options.
- **[docs/PRIVACY.md](docs/PRIVACY.md)** — what the model provider, the host, your reader and publishers can see.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — why the system is shaped this way, the artifact and record contracts, and the handful of things that look like tunables and are not.
- **[docs/scoring-spec.md](docs/scoring-spec.md)** — how judgements become a ranking: scales, arithmetic, the score floor, assembly order, rejects log.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — running the checks, managing dependencies, and the conventions.

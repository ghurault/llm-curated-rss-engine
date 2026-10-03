# LLM Curated RSS Feeds

[![tests](https://github.com/ghurault/llm-curated-rss-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/ghurault/llm-curated-rss-engine/actions/workflows/tests.yml)
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

## Features

- 🧮 **Built-in scoring system, reusable across feeds**: consequence (impact/importance of an article), topic preference and adjustments combine into one score, with a floor and a hard cap on how many articles are ever published in a day (see [docs/scoring-spec.md](docs/scoring-spec.md)).
- ✍️ **User-defined editorial policy**: preferences, hard exclusions, penalties and score adjustments, written by hand per feed.
- 🔁 **Duplicate detection by the LLM**: multiple articles covering the same story collapse into one entry, keeping the most substantive version.
- 🎲 **One discretionary "serendipity" pick per day**: room for the rare item that matters but doesn't fit the policy on paper.
- 🔒 **Optional paywall detection**: articles marked subscriber-only in their own schema.org markup are dropped before the cap, so the slot goes to something you can actually read. Off by default, switched on per feed; a page that can't be reached is treated as readable, never gated on a guess.
- 📚 **Multiple feeds from one instance**: each runner has its own sources, policy and published URL, scheduled independently.
- 🤖 **Supported AI providers**: Claude (API).
- ☁️ **Supported host providers**: Cloudflare Workers (free): static asset hosting, no build pipeline needed.
- ⚙️ **Automation via GitHub Actions (free)**: one scheduled job per feed, daily.
- 💸 **Change-aware deploys**: an unchanged feed isn't re-uploaded, so a quiet day costs nothing.
- 📝 **Optional selection rationale**: add a short, model-free explanation of why an article was picked to its description.

## Privacy

This application was designed to be privacy-conscious, and nearly everything below rests on one assumption: **the repository stays private.** From that starting point, here is what each part of the system can see:

- **Sources** — indirectly reveal your preferences, since subscribing to a feed is itself a signal.
  - Sent to the AI model on every run — see below, this is where most of the risk lies.
  - At rest, stored in a private repository.
  - Per-_published_ article, the source feed's name appears in that entry's `<author>` — the full subscription list is never exposed, only the source of whatever gets selected.
- **Editorial policy** — reveals what you care about directly, and is the most sensitive document in the repository.
  - Sent to the AI model on every run.
  - At rest, intended to be stored in a private repository.
  - The published rationale can't leak it: it's built in code from the independent-source count alone, never from model output. Matched topics — the one piece of model output that _would_ leak the policy — are deliberately kept out of the feed and stay in the private selection log instead (`docs/ARCHITECTURE.md`, D5).
- **Model provider** — sees both documents above, and therefore learns your preferences. See ["What leaves the machine"](#scoring-with-claude) for what specifically is sent and Anthropic's training policy on it.
  - The provider is a seam, not a hard dependency (`docs/ARCHITECTURE.md`, D7): swapping to a self-hosted model is one class away, not a rewrite.
- **Publish target** — the feed is public by default, so anyone with the URL could infer your preferences from what's selected.
  - Mitigated at two layers: an unguessable URL, and `robots.txt` plus a `noindex` header so a crawler that fetches anyway still won't index it.
- **RSS reader** — sees the curated feed's URL and content, same as any other subscription.
  - Low incremental risk in the intended use case: since the curated feed is read _alongside_ the raw subscriptions (see above), the reader already sees the same articles and more via those.
- **Source publishers** — see a request for their feed on every run, and nothing more, unless paywall detection is enabled for that runner.
  - With it on, a publisher additionally sees a request for each of their articles the model graded worth considering that day — a much narrower signal than "subscribed to this feed", since it names the individual articles. It is off by default for this reason as well as the traffic.
  - The request carries the configured `User-Agent` and no cookie or credential, so it is not tied to any account with that publisher.

The privacy-maximalist version of this would be a self-hosted LLM with no published feed at all — reading selections from a local file instead.
That's not what's implemented here for convenience, but the architecture doesn't rule it out, and could be investigated in the future.

## Layout

```text
├── feeds/                       one runner per curated feed (your data)
│   └── <name>/
│       ├── config.toml          what this feed reads, spends and publishes to
│       ├── editorial-policy.md  what you care about, written by hand
│       └── sources.opml         subscription list exported from your feed reader
├── eval/
│   ├── corpus/<name>/           one JSONL file per collection day
│   └── labels/<name>/           your notes on how good the picks were
├── state/<name>/                what each stage produced, one file per day (your data)
├── build/<name>/                the feed, ready to upload (git ignored)
├── src/curated_feed/            the package
│   ├── prompt.md                how to judge an article, shipped with the engine
│   ├── config.template.toml     the annotated settings reference, and what to copy
│   └── editorial-policy.template.md  the annotated taste reference, and what to copy
├── docs/                        hand-written documentation
│   ├── SETUP.md                 how to get it running the first time
│   ├── ARCHITECTURE.md          why it is shaped this way, and the contracts
│   ├── scoring-spec.md          how judgements become a ranking
│   └── api/                     generated API reference (git ignored)
├── .github/workflows/           the scheduled run, and the only thing that publishes
├── scripts/                     utility scripts
├── CONTRIBUTING.md              running the checks and the conventions
├── pyproject.toml               package, dependencies and tool configuration
├── requirements.txt             pinned application dependencies (generated)
├── requirements-dev.txt         pinned application and development dependencies (generated)
└── Makefile                     common tasks
```

`eval/corpus/`, `eval/labels/` and `state/` hold personal data and are gitignored — the directories are tracked, their contents are not.
`feeds/` is committed whole, subscription lists included: the repository is private, and the alternative was a copy of every list in a CI secret to keep in step (`docs/ARCHITECTURE.md`, D8).

`docs/scoring-spec.md` is the ranking specification: the scales, the arithmetic, the score floor, the assembly order and the rejects log.
It is written for whoever changes the selection code, and is deliberately **never** loaded into a prompt — the model is not told the floor or the item cap, because knowing them would only tempt it to pre-filter or pad.
What the model _is_ sent is exactly `src/curated_feed/prompt.md` and the runner's `editorial-policy.md`, in that order.
The first ships with the package and no `config.toml` can name it: its response section is the schema the pipeline parses back, so it versions with the code that reads it. `--scoring-prompt PATH` overrides it for one command.

`docs/ARCHITECTURE.md` records why the system is shaped this way, including the artifact and record contracts and the handful of things that look like tunables and are not.

## Getting started

Setting one up is a one-time sequence of its own, and it lives in **[docs/SETUP.md](docs/SETUP.md)** — devcontainer, subscription list, editorial policy, API key, the random name the feed is served from, the Cloudflare Worker, the repository secrets, and the schedule.
Everything below assumes that is done.

**No key is stored in this repository** — credentials are resolved from the environment, and the two offline scoring providers (`stub`, `file`) need none at all.

## Running it

A runner is chosen by pointing at its config, and there is no default:

```bash
curate-fetch --config feeds/<name>/config.toml   # collect today into that feed's corpus
curate-run --config feeds/<name>/config.toml     # today, end to end, stopping at build/
```

`--config` is found the usual way when it is left out, by searching upwards for a `config.toml`, so `cd feeds/<name>` and then plain `curate-run` works as well.
What no longer works is a bare command from the repository root: with nothing to find, it says so rather than picking a feed for you.

`curate-fetch` keeps items published within the lookback window (default 2 days) and ends with a summary: feeds attempted, succeeded, failed and why, and items written.
Broken or dead feeds are reported, not fatal.
Items already in any corpus file are skipped, so re-running the same day adds nothing.

`curate-run` then scores, optionally checks whether the picks are behind a paywall, selects and renders.
Unattended, all of that happens in CI once a day, one job per feed; by hand it is mostly used to replay a past day.

## Tuning the policy

The point of stage boundaries being files is that a past day can be re-run without refetching it.
How far back you start depends on what you changed:

```bash
cd feeds/<name>
curate-run --date 2026-08-08 --from select      # after changing a weight or the item cap
curate-run --date 2026-08-08 --from score …     # after editing the policy: needs a fresh answer
```

Editing a runner's `editorial-policy.md` changes what the model is asked, so it needs a new response.
Editing its `config.toml` changes only the arithmetic, and re-selecting a saved response takes a second.
Either way it is that feed alone: no other runner reads those files, or the state the replay rewrites.

A day can also be scored **without an API key**, which is how the policy was written in the first place:

```bash
curate-export --prompt > day.md          # both policy documents plus the day's candidates
# paste day.md into a chat session, save the JSON reply as response.json
curate-run --date 2026-08-08 --provider file --response response.json
```

Keep notes in `eval/labels/<name>/` — which picks were right, which were misses, and what the policy failed to say.

## Scoring with Claude

`--provider` overrides the config for one run, so a day can be re-scored against a real model, or against none, without editing anything:

```bash
curate-run --date 2026-08-08 --provider anthropic    # the real thing
curate-run --date 2026-08-08 --provider stub         # recency only, calls nothing
```

Model choice is what drives the cost: you can start with Haiku 4.5 to see if it is good enough. If the picks disappoint, considering that judgment is the product, you can consider a more expensive model like Sonnet at a `medium` effort.

**What leaves the machine.** The day's headlines and summaries, which are already public, and both policy documents.
`editorial-policy.md` is the disclosive one — it describes what you care about. Anthropic does not train on API inputs by default; if that is not good enough, the provider is one class behind a protocol (see `docs/ARCHITECTURE.md`, D7).

**When it fails.** A response that does not validate is retried once with the error appended.
A second failure publishes nothing and exits non-zero — the previously published feed stays served, so a bad day costs one missing day, and the raw response is on disk at `state/<name>/response/DATE.json` either way.
A truncated response (`max_tokens`) and a declined request are both hard failures rather than short days.

## Publishing the feed

Rendering stops at the filesystem. `build/` then holds everything the host needs:

```text
build/<name>/robots.txt            asks crawlers not to fetch
build/<name>/_headers              tells the ones that fetch anyway not to index
build/<name>/<path_prefix>/feed.xml
build/<name>/<path_prefix>/index.html
```

One build directory per runner, and one Worker per runner — an upload replaces a Worker's whole asset manifest, so two feeds sharing one would each delete the other (`docs/ARCHITECTURE.md`, D8).

The reference host is a [Cloudflare Worker serving static assets](https://developers.cloudflare.com/workers/static-assets/) on the free plan, not Cloudflare Pages, which mints a new public `<hash>.<project>.pages.dev` address on every deployment — a bad fit for a feed whose privacy is its URL. Nothing needs creating at the host beforehand: the first upload creates the Worker under the name in the runner's config (steps 7–8 of [docs/SETUP.md](docs/SETUP.md)).

**Only CI publishes.** [`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs the pipeline and uploads — one job per feed, at 06:00 UTC daily or on manual dispatch (narrowable to a single feed; every run spends model credits). `curate-run` itself stops at `build/` unless `--deploy` is given, and the devcontainer holds no deploy credential — so nothing run by hand can reach the internet by accident. Rehearse an upload without one:

```bash
curate-deploy --dry-run --provider wrangler   # prints the wrangler command, runs nothing
```

**An unchanged feed is not re-uploaded.** Render and deploy each record a content hash, so a quiet day costs nothing and the Worker's deployment history stays a list of real changes. `--force` overrides that — use it after changing something the hash doesn't cover, such as `_headers`.

**Expect the reader to lag by hours.** The scheduled run can start minutes late, and the upload itself takes seconds — the real delay is Feedly's own poll interval, which for a low-traffic feed sits in the least-favoured, hours-not-minutes bucket. Refresh the source by hand in the Feedly UI when you want it sooner.

**Late and missing look the same.** An entry carries its article's publication date, not the run time, so with a two-day lookback a fresh entry can arrive already dated two days ago and sort below what you've read. Check the Actions log and `state/<name>/feed.json` before assuming something didn't publish.

## Commands

`curate-template config|policy` — print one of the two references a new runner is copied from, to be redirected into its directory.
Both ship with the engine and nothing reads them at runtime.

`curate-validate PATH` — check a runner's configuration and the files it names, where `PATH` is one `config.toml` or a directory whose subdirectories are runners.
Reads nothing but the filesystem: no feed is resolved and no credential is needed, so it is safe as a pull-request gate and as an unattended preflight.
Exits non-zero with one line per problem.

`curate-fetch` — fetch feeds into today's corpus file.

| flag                    | effect                                                |
| ----------------------- | ----------------------------------------------------- |
| `--days N`              | lookback window in days (`0` disables date filtering) |
| `--sources PATH`        | OPML file to read                                     |
| `--corpus-dir PATH`     | where corpus files are written                        |
| `--summary-max-chars N` | summary truncation limit                              |
| `--concurrency N`       | maximum concurrent requests                           |
| `--user-agent STRING`   | `User-Agent` header                                   |
| `--config PATH`         | config file to use                                    |

`curate-export [FILE]` — render a corpus file for review, defaulting to the most recent one.

| flag                | effect                                                   |
| ------------------- | -------------------------------------------------------- |
| `--prompt`          | emit the whole scoring prompt, policy documents included |
| `--max-items N`     | render at most this many items                           |
| `--out PATH`        | write to a file instead of stdout                        |
| `--corpus-dir PATH` | where to look for corpus files                           |
| `--config PATH`     | config file to use                                       |

`curate-score` — ask a scorer to judge one day.

| flag              | effect                                                  |
| ----------------- | ------------------------------------------------------- |
| `--date DAY`      | collection day to score (default: the most recent)      |
| `--provider NAME` | `stub`, `file` or `anthropic`; overrides the config     |
| `--response PATH` | saved response to read, required by the `file` provider |
| `--config PATH`   | config file to use                                      |

`curate-paywall` — check whether the day's graded articles can be read, and record a verdict for each.
Does nothing unless `[paywall] enabled` is set for the runner.
`curate-select` — score a saved response and assemble the day's selection.
`curate-render` — publish that selection into the rolling feed under `build/`.
All three take `--date` and `--config`; the first two also take `--corpus-dir`.

`curate-deploy` — upload `build/` to the host the feed is served from.

| flag               | effect                                                       |
| ------------------ | ------------------------------------------------------------ |
| `--provider NAME`  | `none` or `wrangler`; overrides the config                   |
| `--dry-run`        | print the upload that would run, and upload nothing          |
| `--force`          | upload even when the feed has not changed since the last one |
| `--build-dir PATH` | directory to upload                                          |
| `--config PATH`    | config file to use                                           |

`curate-run` — the whole chain, in order.

| flag              | effect                                                               |
| ----------------- | -------------------------------------------------------------------- |
| `--date DAY`      | replay a past day; skips fetching, since it is past                  |
| `--from STAGE`    | start at `fetch`, `score`, `paywall`, `select`, `render` or `deploy` |
| `--deploy`        | publish as well, instead of stopping at `build/`                     |
| `--provider NAME` | passed to the scoring stage                                          |
| `--response PATH` | passed to the scoring stage                                          |
| `--config PATH`   | config file to use                                                   |

## Reference

- **[docs/SETUP.md](docs/SETUP.md)** — getting a working instance from a fresh clone, in ten steps, and what to check when one of them is wrong.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — why the system is shaped this way, the artifact and record contracts, and the handful of things that look like tunables and are not.
- **[docs/scoring-spec.md](docs/scoring-spec.md)** — how judgements become a ranking: scales, arithmetic, the score floor, assembly order, rejects log.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — running the checks, managing dependencies, and the conventions.

# Setup

Getting a working instance of your own, with one curated feed.
Eleven steps, in order, and the order matters: each one is cheap to undo except the name in step 8, which is baked into the feed's identity.
A second feed is a shorter list, at the end.

Nothing here is needed twice.
For running it day to day, see [README.md](../README.md); for why it is shaped this way, [ARCHITECTURE.md](ARCHITECTURE.md).

**You will need**: a feed reader that can export OPML and subscribe to an arbitrary URL, Docker, a GitHub account for a private repository, a Cloudflare account for the free Workers tier — an account is all, since nothing has to be created in its dashboard — and, only from step 7, an Anthropic API account with prepaid credits.

---

## 1. Create your configuration repository

This repository is the engine, and you do not fork it.
Your feeds live in a repository of your own, which calls the engine's published image and holds everything that describes you: subscription lists, editorial policies, evaluation notes.
**Make it private** — the editorial policy is the most disclosive document in the project (see [ARCHITECTURE.md](ARCHITECTURE.md), D10).

By the end of this guide it will look like this:

```text
├── feeds/
│   └── <name>/                  one runner per curated feed
│       ├── config.toml          what this feed reads, spends and publishes to
│       ├── editorial-policy.md  what you care about, written by hand
│       └── sources.opml         subscription list exported from your feed reader
├── eval/
│   ├── corpus/<name>/           one JSONL file per collection day (ignored)
│   └── labels/<name>/           your notes on how good the picks were (ignored)
├── state/<name>/                what each stage produced (ignored)
├── build/<name>/                the feed, ready to upload (ignored)
└── .github/workflows/daily.yml  the schedule, the matrix and the credentials
```

The layout is not a suggestion: the engine's workflow reads `feeds/<name>/config.toml` and caches `eval/corpus/<name>` and `state/<name>`, and the config template's paths assume the same.
Start the repository with a `.gitignore` that keeps what the pipeline produces out of git:

```gitignore
build/
state/
eval/corpus/
eval/labels/
day.md
response.json
```

## 2. Get the engine

Every command runs from the engine's image, the same one CI runs.
A shell function saves typing it out:

```bash
engine() {
  docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp -e ANTHROPIC_API_KEY \
    -v "$PWD:/w" -w /w ghcr.io/ghurault/llm-curated-rss-engine:latest "$@"
}
```

It mounts the current directory and nothing else, so **run every command from the root of your configuration repository**: a runner's paths point at `eval/` and `state/` beside `feeds/`, and from anywhere lower they would be outside the mount.
`--user` keeps what it writes owned by you rather than by the container's root.
`latest` is fine by hand; CI pins an exact image instead (step 10).

Check it took:

```bash
engine curate-fetch --help
```

Without Docker, `pip install "curated-feed @ git+https://github.com/ghurault/llm-curated-rss-engine"` into Python 3.12 or later gives the same commands; drop the `engine` prefix below.
Only rehearsing an upload (step 9) then needs Node on top.

## 3. Create the runner and add your subscription list

A curated feed is a directory under `feeds/`, and the config in it is what defines the feed: what it reads, whose taste it applies, which model it pays for, which Worker serves it.
Copy the annotated reference, and replace every `<name>` in it with the directory's own name:

```bash
mkdir -p feeds/<name>
engine curate-template config > feeds/<name>/config.toml
```

Then export OPML from your feed reader (in Feedly: _Settings → Feeds → Export OPML_) and save it as `feeds/<name>/sources.opml`.
Folder structure is preserved and recorded per feed.

**Commit it.** The repository is private, and the subscription list is what CI reads directly — there is no copy of it anywhere else to keep in step.

`engine curate-validate feeds/<name>/config.toml` says whether what you have written so far hangs together.
It reads the config and the files it names, and nothing else — no feed is resolved and no credential is needed — so it is worth running after every edit here.

Every command below takes `--config feeds/<name>/config.toml`.
There is no default: with nothing to point at, a command says so rather than picking a feed for you.

## 4. Collect a few days

```bash
engine curate-fetch --config feeds/<name>/config.toml
```

This fetches every feed, keeps items published within the lookback window (default 2 days), and appends them to `eval/corpus/<name>/YYYY-MM-DD.jsonl`.
It ends with a summary: feeds attempted, succeeded, failed and why, and items written.
Broken or dead feeds are reported, not fatal.

Run it once a day for a few days before going further.
Items already in any corpus file are skipped, so re-running the same day adds nothing and the next day appends a new file.
A few real days is what the next two steps are tuned against; one day is not enough to tell a good policy from a lucky one.

## 5. Write the editorial policy

`feeds/<name>/editorial-policy.md` is where that feed's taste goes: the topics you follow, what is excluded outright, what is penalised.
Copy the annotated reference and replace the placeholders:

```bash
engine curate-template policy > feeds/<name>/editorial-policy.md
```

Nothing validates the result, so read the comment block at the top of it before deleting it: the two conventions the prompt actually depends on are that a rule is named after its bolded bullet heading, and that the document states which wins when an exclusion and a topic match collide.

[`src/curated_feed/prompt.md`](../src/curated_feed/prompt.md) says how to judge an article; it ships with the engine, is shared by every feed, and is not yours to edit — `--scoring-prompt PATH` tries a different one for the length of a command.

Then score a day **without an API key**, by hand, and iterate:

```bash
engine curate-export --config feeds/<name>/config.toml --prompt > day.md
# paste day.md into a chat session, save the JSON reply as response.json
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider file --response response.json
```

That runs the real scoring, selection and rendering against the model's answer.
Keep notes in `eval/labels/<name>/` — which picks were right, which were misses, and what the policy failed to say.
This loop is the whole point of the project and is worth several rounds; step 7 only automates a policy you already trust.

## 6. Prove the feed in your reader

Before spending anything on a model, confirm the plumbing.
The stub scorer publishes the most recent items and calls nothing:

```bash
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider stub
```

`build/<name>/` now holds the feed. Serve it anywhere your reader can reach — the Worker in step 9 is one way, and doing that step early with stub output is a reasonable order.
Subscribe, then confirm that items appear once, in the right order, and do not resurface as unread after a second run.

## 7. Give it an API key

API billing is prepaid credits bought in the [Console](https://console.anthropic.com), and is **separate from a Claude.ai subscription** — Pro and Max grant no API credits.

Nothing is read from the config file; the SDK resolves the credential from the environment, and the `engine` function passes it into the container:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Then point that runner's config at the provider — each feed names its own model, so a low-volume feed can run a cheaper one:

```toml
[score]
provider = "anthropic"
model = "claude-sonnet-5"
effort = "medium"
```

`effort` trades tokens for judgement — `low`, `medium`, `high`, `xhigh`, `max`.
It must be left **empty** for Haiku 4.5, which rejects the parameter outright; every other model here accepts it.

Re-score a day you have already labelled, and compare:

```bash
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from score --provider anthropic
```

## 8. Choose the name the feed is served from

The feed's `<id>` and `<link rel="self">` are baked in at render time, and the Worker's name is the first label of its hostname, so **the URL is decided before the first upload**.
Changing it afterwards makes your reader treat every published item as new.

```bash
python3 -c "import secrets; print(secrets.token_hex(4), secrets.token_hex(8))"
```

The first value is the Worker's name. It must not be descriptive: it is the whole secret, since the rest of the hostname is your account's Workers subdomain, which is shared by everything you ever deploy there.

The second value is optional — a path segment under that hostname, giving a second independent secret rather than one.
Both go in `feeds/<name>/config.toml`, and no two runners may share either: an upload replaces a Worker's whole asset manifest, so two feeds pointed at one Worker would each delete the other.

```toml
[publish]
site_url = "https://<worker>.<account-subdomain>.workers.dev"
path_prefix = ""   # or the second random value

[deploy]
provider = "wrangler"
project = "<worker>"
```

`site_url` is the origin alone, with no path and no trailing slash.
`<account-subdomain>` is not yours to choose: Cloudflare assigns one per account and shows it in the dashboard alongside Workers & Pages.
It is the same for every Worker in the account, so a second feed only needs a new first label.

An empty `path_prefix` serves the feed at `/feed.xml`; a filled one at `/<segment>/feed.xml`.
Either is fine, but decide now — adding one later moves the feed.

`compatibility_date` can be left at its default.
It pins a Workers runtime version, which changes nothing for a Worker that is only static files, but wrangler asks for one and asks interactively, which in CI would hang.

Then render once more, so the identity in the feed is the final one:

```bash
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from render
```

## 9. Where the feed is served from

The reference host is [Cloudflare Workers with static assets](https://developers.cloudflare.com/workers/static-assets/) on the free plan.
Static-asset requests are unmetered, and the configuration repository is private, so there is nothing for a Git integration to build.

**There is nothing to create.** `wrangler deploy` creates a Worker that does not exist yet, so the name you chose in step 8 is all a runner needs: the first upload — which is CI's, in step 10 — brings the Worker into existence and serves the feed from it.
All you need in the dashboard is an account whose Workers subdomain has been assigned, which happens once per account rather than once per feed.
This is why a second feed costs a random name and two settings, and no clicking.

If you would rather see the feed in your reader before CI has any credentials, the dashboard does the same job by hand: **Workers & Pages → Create → Upload assets**, enter the name from step 8, drop in the `build/<name>/` folder, and deploy.
Later uploads from CI update that same Worker in place.

> **Not Cloudflare Pages**, which is the other half of that dashboard section and does the same job.
> Pages mints a permanent `<hash>.<project>.pages.dev` address on every deployment, so a daily job accumulates public addresses for a feed whose privacy is its URL.
> `wrangler deploy` updates one address in place, which is what the engine runs.

Once something has been uploaded, check the resulting `https://<worker>.<account-subdomain>.workers.dev` against the `site_url` set in step 8, then subscribe to `<site_url>/feed.xml` — or `<site_url>/<path_prefix>/feed.xml` — and confirm your reader ingests it.

To rehearse what CI will do — the image carries the wrangler release CI uses:

```bash
engine curate-deploy --config feeds/<name>/config.toml --dry-run --provider wrangler   # prints the command, runs nothing
```

## 10. Give CI its workflow and secrets

The scheduled run is the only thing that publishes, and it is two workflows: yours, which owns the clock, the matrix and the credentials, and the engine's [`curate.yml`](../.github/workflows/curate.yml), which runs one feed's day inside the image and is called once per feed.
Save this as `.github/workflows/daily.yml`, replacing `<name>` and `<commit>`:

```yaml
name: daily

on:
  workflow_dispatch:
    inputs:
      feed:
        description: Which feed to curate
        type: choice
        options: [all, <name>] # keep in step with the matrix below
        default: all
      provider:
        description: Scorer to use
        type: choice
        options: [anthropic, stub]
        default: anthropic
      date:
        description: Replay a past collection day (YYYY-MM-DD); empty means today
        type: string
        default: ""
  schedule:
    - cron: "0 6 * * *"

permissions:
  contents: read

jobs:
  curate:
    strategy:
      fail-fast: false
      matrix:
        feed: ${{ fromJSON((inputs.feed == null || inputs.feed == 'all') && '["<name>"]' || format('["{0}"]', inputs.feed)) }}
    concurrency:
      group: daily-${{ matrix.feed }}
    uses: ghurault/llm-curated-rss-engine/.github/workflows/curate.yml@<commit>
    with:
      feed: ${{ matrix.feed }}
      provider: ${{ inputs.provider || 'anthropic' }}
      date: ${{ inputs.date || '' }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
      CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
```

The lines that are not obvious:

- **`@<commit>` is a full commit SHA from this repository's `main`, never a branch or `latest`.** That commit's `curate.yml` pins an image by digest, so the SHA fixes exactly which engine your 06:00 run uses. Moving it is how you take an engine release, and nothing pushed here reaches you until you do.
- **`fail-fast: false`**, because one feed's bad day — a model timeout, a malformed response — must not cancel another that would have published.
- **`concurrency` per feed**, so two feeds never queue behind each other, while two runs of the same feed cannot both append to its published log. It has to be on this side: a group named in a called workflow is scoped to whoever called it.
- **The `||` fallbacks**, because the dispatch form's defaults do not apply to a scheduled run, where every input is null. That null is also why the matrix expression treats a missing `feed` as `all`.
- **Secrets named one by one** rather than `secrets: inherit`, which would hand somebody else's workflow every secret your repository holds.

The corpus and the published log are kept between runs in your repository's Actions cache, by `curate.yml`; there is nothing to set up.

Under **Settings → Secrets and variables → Actions**, add three secrets:

| secret                  | what it is                                                                             |
| ----------------------- | -------------------------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`     | the scoring credential                                                                 |
| `CLOUDFLARE_API_TOKEN`  | an API token from the **Edit Cloudflare Workers** template, scoped to that one account |
| `CLOUDFLARE_ACCOUNT_ID` | from the dashboard sidebar                                                             |

All three are shared by every feed: the token is account-scoped, so it covers a Worker that does not exist yet, and nothing here has to change when a feed is added.
There is no subscription-list secret — the list is committed (step 3), which is one copy fewer to keep in step.
These three are the only ones `curate.yml` declares, and a secret passed to it that it does not declare is silently dropped.

**Repository secrets, not environment secrets.**
`curate.yml` names no environment, so secrets put in one resolve to the empty string with no warning at all: the run fails several steps later on a rejected credential.

Run the workflow once by hand — **Actions → daily → Run workflow** — with the `stub` provider, to prove the credentials without spending.
Then run it with `anthropic`.
The first run is also what creates the Worker.

## 11. Turn on the schedule

The workflow above has its schedule live, one job per feed:

```yaml
schedule:
  - cron: "0 6 * * *"
```

Every unattended run spends model credits for every feed in the matrix, so comment this out until the policy is worth paying for, and dispatch by hand in the meantime.

06:00 UTC is late enough that yesterday's feeds have settled, and early enough to be there at breakfast — though it is when the job starts, not when a reader shows the result.

---

## Adding another feed

Nothing above is repeated. A second feed is its own runner, and shares the engine, the credentials and the packaged scoring prompt with the first:

1. `mkdir -p feeds/<name>` and `engine curate-template config > feeds/<name>/config.toml`, replacing every `<name>`.
2. `engine curate-template policy > feeds/<name>/editorial-policy.md` and write it, and export its `sources.opml` (steps 3 and 5). Commit both.
3. Generate a Worker name and path segment (step 8) and set `[deploy] project` and `[publish] site_url`. They must not be the first feed's: an upload replaces a Worker's whole asset manifest, so two feeds pointed at one Worker would each delete the other.
4. Add `<name>` to the `matrix` in `daily.yml`, and to the `feed` dispatch input's options beside it.
5. `engine curate-validate feeds/` — the half-edited copy is what this catches, and it costs nothing to run.
6. Prove it as in step 6, with the stub scorer, before letting the schedule pay for it.

`curate-validate feeds/` checks the parts of that a person forgets: that no two runners share a state directory, corpus, build directory, Worker or feed URL, that every runner has a subscription list with feeds in it, and that its editorial policy has actually been written.
It cannot see the workflow, so a runner left out of the matrix is valid and simply never runs.

---

## When something is wrong

| symptom                                                                  | likely cause                                                                                                           |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| Reader shows every item as unread again                                  | `path_prefix` or `site_url` changed after publishing; entry ids are the article URLs, so this means the feed URL moved |
| The run succeeded but nothing was uploaded                               | the feed hash has not moved since the last upload; `curate-deploy --force` overrides that                              |
| The first CI upload asks a question and hangs                            | wrangler is prompting about something a flag did not answer; the job log shows which                                   |
| A job stops at "OPML file not found"                                     | that runner's `sources.opml` was never committed, or its `[paths] sources` points elsewhere                            |
| A command by hand cannot find a file the config names                    | it was run from below the repository root, so the `engine` mount does not reach `eval/` or `state/`                    |
| CI curates feeds you have unsubscribed from                              | the committed `sources.opml` is stale — re-export it                                                                   |
| CI fails on a rejected credential                                        | the secrets are in an environment rather than on the repository (step 10)                                              |
| One feed publishes another's items, or an upload replaces the wrong feed | two runners share a `state_dir`, `build_dir` or `[deploy] project`; `curate-validate feeds/` names which               |
| A feed stops updating while the others are fine                          | that job failed on its own; `fail-fast` is off, so the others still published — read the job, not the run              |
| A shorter feed than usual after a CI outage                              | the Actions cache holding the corpus was evicted; it refills tomorrow, and nothing already read comes back             |
| `curate-score` exits non-zero                                            | the model's answer failed validation twice; the raw text is on disk at `state/<name>/response/DATE.json`               |

# Setup

Getting your own instance running, with one curated feed.
For day-to-day use see [README.md](../README.md); for the reasoning, [ARCHITECTURE.md](ARCHITECTURE.md).

> **A template configuration repository is coming soon.**
> It will make onboarding easier and supersede most of this guide.

**You will need**: a feed reader that exports OPML, Docker, a GitHub account, a Cloudflare account (free Workers tier), and from step 7 an Anthropic API account with prepaid credits.

Do the steps in order. All of them can be undone cheaply except step 8: the feed's URL is part of its identity.

---

## 1. Create your configuration repository

Don't fork this repository: it is the engine.
Your feeds go in a **private** repository of your own, because the editorial policy describes what you care about (see [ARCHITECTURE.md](ARCHITECTURE.md), D10).
Use this layout, which the engine's workflow and the config template both rely on:

```text
├── feeds/<name>/                one directory per feed
│   ├── config.toml
│   ├── editorial-policy.md
│   └── sources.opml
├── eval/corpus/<name>/          collected items (ignored)
├── eval/labels/<name>/          your notes on the picks (ignored)
├── state/<name>/                what each stage produced (ignored)
├── build/<name>/                the feed, ready to upload (ignored)
└── .github/workflows/daily.yml  the schedule, the matrix and the credentials
```

```gitignore
build/
state/
eval/corpus/
eval/labels/
day.md
response.json
```

## 2. Get the engine

Commands run from the engine's published image:

```bash
engine() {
  docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp -e ANTHROPIC_API_KEY \
    -v "$PWD:/w" -w /w ghcr.io/ghurault/llm-curated-rss-engine:latest "$@"
}
engine curate-fetch --help
```

**Run every command from the repository root.** The function mounts only the current directory, and a runner's paths reach `eval/` and `state/` beside `feeds/`.

If you don't want Docker, `pip install "curated-feed @ git+https://github.com/ghurault/llm-curated-rss-engine"` (Python 3.12+) installs the same commands; leave out the `engine` prefix.

## 3. Create the runner

```bash
mkdir -p feeds/<name>
engine curate-template config > feeds/<name>/config.toml   # then replace every <name>
```

Export OPML from your feed reader to `feeds/<name>/sources.opml` (in Feedly: _Settings → Feeds → Export OPML_), and **commit it**: CI reads the list directly.

Run `engine curate-validate feeds/<name>/config.toml` after every edit.
It needs no network and no credentials.
Every command takes `--config feeds/<name>/config.toml`; nothing defaults to a feed.

## 4. Collect a few days

```bash
engine curate-fetch --config feeds/<name>/config.toml
```

This appends the lookback window (2 days by default) to `eval/corpus/<name>/YYYY-MM-DD.jsonl` and reports which feeds failed.
Run it daily for a few days, because one day is too little to tune a policy against.
Re-running a day adds nothing.

## 5. Write the editorial policy

```bash
engine curate-template policy > feeds/<name>/editorial-policy.md
```

Read the comment block at the top before deleting it, because nothing validates the policy.
The prompt depends on two conventions:

- a rule's name is its bolded bullet heading;
- the policy says which wins when an exclusion and a topic collide.

The scoring prompt, [`prompt.md`](../src/curated_feed/prompt.md), ships with the engine. To try a different one for a single command, use `--scoring-prompt PATH`.

Score a day **without an API key**, and iterate:

```bash
engine curate-export --config feeds/<name>/config.toml --prompt > day.md
# paste day.md into a chat session, save the JSON reply as response.json
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider file --response response.json
```

Keep notes in `eval/labels/<name>/` on the right picks, the misses, and what the policy failed to say.
This loop is the point of the project. Step 7 only automates a policy you already trust.

## 6. Prove the feed in your reader

```bash
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider stub
```

The stub scorer calls nothing.
Serve `build/<name>/` anywhere your reader can reach (step 9 is one way), subscribe to it, and check two things:

- items appear once, in order;
- they don't come back as unread after a second run.

## 7. Give it an API key

API credits are bought in the [Console](https://console.anthropic.com) and are **separate from a Claude.ai subscription**.
The key comes from the environment, never from the config:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

```toml
[score]
provider = "anthropic"
model = "claude-sonnet-5"
effort = "medium"   # leave empty for Haiku 4.5, which rejects it
```

Then re-score a day you have labelled, and compare:

```bash
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from score --provider anthropic
```

## 8. Choose the feed's URL

The URL is baked into the feed, so changing it after the first upload makes your reader treat every item as new.

```bash
python3 -c "import secrets; print(secrets.token_hex(4), secrets.token_hex(8))"
```

- The first value is the Worker's name. It is the secret part of the hostname, so it must not be descriptive.
- The second is an optional path segment, which adds a second secret.

No two runners may share either, because an upload replaces a Worker's whole contents.

```toml
[publish]
site_url = "https://<worker>.<account-subdomain>.workers.dev"   # origin only, no trailing slash
path_prefix = ""   # or the second value

[deploy]
provider = "wrangler"
project = "<worker>"
```

`<account-subdomain>` is assigned by Cloudflare and shown in the dashboard under Workers & Pages.
Re-render so that the feed carries its final identity:

```bash
engine curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from render
```

## 9. The host

The feed is served by a [Cloudflare Worker with static assets](https://developers.cloudflare.com/workers/static-assets/) on the free plan.
**There is nothing to create.** CI's first upload (step 10) creates the Worker under the name from step 8.

To see the feed before CI is set up, go to **Workers & Pages → Create → Upload assets**, use that same name, and upload `build/<name>/`.
Don't use Cloudflare Pages: it mints a new public address on every deployment.

Subscribe to `<site_url>/feed.xml`, or `<site_url>/<path_prefix>/feed.xml`.
To rehearse what CI will run without uploading anything:

```bash
engine curate-deploy --config feeds/<name>/config.toml --dry-run --provider wrangler
```

## 10. Set up CI

Your workflow owns the schedule, the matrix and the secrets. It calls the engine's [`curate.yml`](../.github/workflows/curate.yml) once per feed.
Save this as `.github/workflows/daily.yml`:

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

- **`<commit>` is a full SHA from this repository's `main`.** That commit pins the engine image, so your daily run changes only when you move the SHA.
- **The `||` fallbacks** are needed because the form's defaults don't apply to scheduled runs, where every input is null.
- **`concurrency` must stay in your workflow.** A group named inside a called workflow is scoped to the caller.

The corpus and the published log persist in your repository's Actions cache; there is nothing to set up.
Add three **repository** secrets under **Settings → Secrets and variables → Actions**. Don't put them in an environment: `curate.yml` names none, so they would resolve to empty strings without any warning.

| secret                  | what it is                                                      |
| ----------------------- | --------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`     | the scoring credential                                          |
| `CLOUDFLARE_API_TOKEN`  | from the **Edit Cloudflare Workers** template, for that account |
| `CLOUDFLARE_ACCOUNT_ID` | from the dashboard sidebar                                      |

Dispatch the workflow by hand (**Actions → daily → Run workflow**), first with `stub` to prove the credentials, then with `anthropic`.

## 11. The schedule

The workflow runs at 06:00 UTC, and each run spends credits for every feed.
Comment out the `schedule` until the policy is worth paying for.

---

## Adding another feed

1. `mkdir -p feeds/<name>`, `curate-template config` and `curate-template policy` into it, a `sources.opml`, and commit (steps 3 and 5).
2. A new Worker name and path segment (step 8). Never reuse another feed's.
3. Add `<name>` to the matrix and to the dispatch options in `daily.yml`.
4. `engine curate-validate feeds/`, which catches runners that share a state directory, corpus, build directory, Worker or URL. It can't see the workflow, so it won't notice a runner missing from the matrix.
5. Prove it with the stub scorer (step 6).

## When something is wrong

| symptom                                     | likely cause                                                                         |
| ------------------------------------------- | ------------------------------------------------------------------------------------ |
| Every item shows as unread again            | `site_url` or `path_prefix` changed after publishing                                 |
| The run succeeded but nothing was uploaded  | the feed has not changed since the last upload; `curate-deploy --force` overrides    |
| The first CI upload hangs                   | wrangler is prompting; the job log shows for what                                    |
| "OPML file not found"                       | `sources.opml` was never committed, or `[paths] sources` points elsewhere            |
| A command run by hand cannot find a file    | it was run below the repository root, outside the `engine` mount                     |
| CI curates feeds you have unsubscribed from | the committed `sources.opml` is stale; re-export it                                  |
| CI fails on a rejected credential           | the secrets are in an environment, not on the repository                             |
| One feed publishes another's items          | two runners share a `state_dir`, `build_dir` or Worker; `curate-validate` says which |
| One feed stops updating                     | its job failed on its own; read that job's log                                       |
| A shorter feed after a CI outage            | the Actions cache was evicted; it refills, and nothing already read comes back       |
| `curate-score` exits non-zero               | the answer failed validation twice; the raw text is in `state/<name>/response/`      |

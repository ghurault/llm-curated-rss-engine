# Setup

Getting a working instance from a fresh clone, with one curated feed.
Ten steps, in order, and the order matters: each one is cheap to undo except the name in step 7, which is baked into the feed's identity.
A second feed is a shorter list, at the end.

Nothing here is needed twice.
For running it day to day, see [README.md](../README.md); for why it is shaped this way, [ARCHITECTURE.md](ARCHITECTURE.md).

**You will need**: a feed reader that can export OPML and subscribe to an arbitrary URL, a GitHub account for the private repository, a Cloudflare account for the free Workers tier — an account is all, since nothing has to be created in its dashboard — and, only from step 6, an Anthropic API account with prepaid credits.

---

## 1. Open the repository in the devcontainer

In VS Code: _Dev Containers: Reopen in Container_.
It is built from the repository's own `Dockerfile`, which holds every pinned dependency; creating the container installs the development set, the local package in editable mode and the git hooks.
It is a development environment and nothing else — the scheduled run happens in CI, from the other stage of that same file.

Without VS Code, the container works directly:

```bash
docker build --target devcontainer -t curated-feed . && \
  docker run --rm -it -v "$PWD:/work" -w /work curated-feed bash -lc "make init && bash"
```

Check it took:

```bash
curate-fetch --help
```

## 2. Create the runner and add your subscription list

A curated feed is a directory under `feeds/`, and the config in it is what defines the feed: what it reads, whose taste it applies, which model it pays for, which Worker serves it.
Copy the annotated reference, and replace every `<name>` in it with the directory's own name:

```bash
mkdir -p feeds/<name>
curate-template config > feeds/<name>/config.toml
```

Then export OPML from your feed reader (in Feedly: _Settings → Feeds → Export OPML_) and save it as `feeds/<name>/sources.opml`.
Folder structure is preserved and recorded per feed.

**Commit it.** The repository is private, and the subscription list is what CI reads directly — there is no copy of it anywhere else to keep in step.

`curate-validate feeds/<name>/config.toml` says whether what you have written so far hangs together.
It reads the config and the files it names, and nothing else — no feed is resolved and no credential is needed — so it is worth running after every edit here.

Every command below takes `--config feeds/<name>/config.toml`.
Since a config is also found by searching upwards, `cd feeds/<name>` once and the flag can be dropped; the examples spell it out.

## 3. Collect a few days

```bash
curate-fetch --config feeds/<name>/config.toml
```

This fetches every feed, keeps items published within the lookback window (default 2 days), and appends them to `eval/corpus/<name>/YYYY-MM-DD.jsonl`.
It ends with a summary: feeds attempted, succeeded, failed and why, and items written.
Broken or dead feeds are reported, not fatal.

Run it once a day for a few days before going further.
Items already in any corpus file are skipped, so re-running the same day adds nothing and the next day appends a new file.
A few real days is what the next two steps are tuned against; one day is not enough to tell a good policy from a lucky one.

## 4. Write the editorial policy

`feeds/<name>/editorial-policy.md` is where that feed's taste goes: the topics you follow, what is excluded outright, what is penalised.
Copy the annotated reference and replace the placeholders:

```bash
curate-template policy > feeds/<name>/editorial-policy.md
```

Nothing validates the result, so read the comment block at the top of it before deleting it: the two conventions the prompt actually depends on are that a rule is named after its bolded bullet heading, and that the document states which wins when an exclusion and a topic match collide.

`src/curated_feed/prompt.md` says how to judge an article; it ships with the engine, is shared by every feed, and is not yours to edit — `--scoring-prompt PATH` tries a different one for the length of a command.

Then score a day **without an API key**, by hand, and iterate:

```bash
cd feeds/<name>
curate-export --prompt > day.md          # both policy documents plus the day's candidates
# paste day.md into a chat session, save the JSON reply as response.json
curate-run --date 2026-08-08 --provider file --response response.json
```

That runs the real scoring, selection and rendering against the model's answer.
Keep notes in `eval/labels/<name>/` — which picks were right, which were misses, and what the policy failed to say.
This loop is the whole point of the project and is worth several rounds; step 6 only automates a policy you already trust.

## 5. Prove the feed in your reader

Before spending anything on a model, confirm the plumbing.
The stub scorer publishes the most recent items and calls nothing:

```bash
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider stub
```

`build/<name>/` now holds the feed. Serve it anywhere your reader can reach — the Worker in step 8 is one way, and doing that step early with stub output is a reasonable order.
Subscribe, then confirm that items appear once, in the right order, and do not resurface as unread after a second run.

## 6. Give it an API key

API billing is prepaid credits bought in the [Console](https://console.anthropic.com), and is **separate from a Claude.ai subscription** — Pro and Max grant no API credits.

Nothing is read from the config file; the SDK resolves the credential itself:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or:
ant auth login                        # stores a profile the SDK picks up
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
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from score --provider anthropic
```

## 7. Choose the name the feed is served from

The feed's `<id>` and `<link rel="self">` are baked in at render time, and the Worker's name is the first label of its hostname, so **the URL is decided before the first upload**.
Changing it afterwards makes your reader treat every published item as new.

```bash
python -c "import secrets; print(secrets.token_hex(4), secrets.token_hex(8))"
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
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from render
```

## 8. Where the feed is served from

The reference host is [Cloudflare Workers with static assets](https://developers.cloudflare.com/workers/static-assets/) on the free plan.
Static-asset requests are unmetered, and the repository is private, so there is nothing for a Git integration to build.

**There is nothing to create.** `wrangler deploy` creates a Worker that does not exist yet, so the name you chose in step 7 is all a runner needs: the first upload — which is CI's, in step 9 — brings the Worker into existence and serves the feed from it.
All you need in the dashboard is an account whose Workers subdomain has been assigned, which happens once per account rather than once per feed.
This is why a second feed costs a random name and two settings, and no clicking.

If you would rather see the feed in your reader before CI has any credentials, the dashboard does the same job by hand: **Workers & Pages → Create → Upload assets**, enter the name from step 7, drop in the `build/<name>/` folder, and deploy.
Later uploads from CI update that same Worker in place.

> **Not Cloudflare Pages**, which is the other half of that dashboard section and does the same job.
> Pages mints a permanent `<hash>.<project>.pages.dev` address on every deployment, so a daily job accumulates public addresses for a feed whose privacy is its URL.
> `wrangler deploy` updates one address in place, which is what `deploy.py` runs.

Once something has been uploaded, check the resulting `https://<worker>.<account-subdomain>.workers.dev` against the `site_url` set in step 7, then subscribe to `<site_url>/feed.xml` — or `<site_url>/<path_prefix>/feed.xml` — and confirm your reader ingests it.

To rehearse what CI will do, from the devcontainer, which has Node for exactly this:

```bash
curate-deploy --dry-run --provider wrangler   # prints the wrangler command, runs nothing
```

## 9. Give CI its secrets

[`.github/workflows/daily.yml`](../.github/workflows/daily.yml) is the only thing that publishes.
Under **Settings → Secrets and variables → Actions**, add three secrets:

| secret                  | what it is                                                                             |
| ----------------------- | -------------------------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`     | the scoring credential                                                                 |
| `CLOUDFLARE_API_TOKEN`  | an API token from the **Edit Cloudflare Workers** template, scoped to that one account |
| `CLOUDFLARE_ACCOUNT_ID` | from the dashboard sidebar                                                             |

All three are shared by every feed: the token is account-scoped, so it covers a Worker that does not exist yet, and nothing here has to change when a feed is added.
There is no subscription-list secret — the list is committed (step 2), which is one copy fewer to keep in step.

**Repository secrets, unless you add an environment yourself.**
Neither `daily.yml` nor the `curate.yml` it calls names an environment, so the three above belong on the repository.
Put them in an environment instead and every secret resolves to the empty string with no warning at all: the run fails several steps later on a rejected credential.
If you want publishing gated behind a protection rule, add an `environment:` line to the job in `curate.yml` — the job in `daily.yml` only calls it, and cannot carry one — and keep its name and the environment in step with each other.

A fourth secret has to be named twice: once where `daily.yml` passes it, and once where `curate.yml` declares it. Passing one that was never declared is silently dropped.

Run the workflow once by hand — **Actions → daily → Run workflow** — with the `stub` provider, to prove the credentials without spending.
Then run it with `anthropic`.
The first run is also what creates the Worker.

## 10. Turn on the schedule

`daily.yml` is committed with its schedule live, one job per feed:

```yaml
schedule:
  - cron: "0 6 * * *"
```

Every unattended run spends model credits for every feed in the matrix, so comment this out until the policy is worth paying for, and dispatch by hand in the meantime.

06:00 UTC is late enough that yesterday's feeds have settled, and early enough to be there at breakfast — though it is when the job starts, not when a reader shows the result.

---

## Adding another feed

Nothing above is repeated. A second feed is its own runner, and shares the code, the credentials and the packaged scoring prompt with the first:

1. `mkdir -p feeds/<name>` and `curate-template config > feeds/<name>/config.toml`, replacing every `<name>`.
2. `curate-template policy > feeds/<name>/editorial-policy.md` and write it, and export its `sources.opml` (steps 2 and 4). Commit both.
3. Generate a Worker name and path segment (step 7) and set `[deploy] project` and `[publish] site_url`. They must not be the first feed's: an upload replaces a Worker's whole asset manifest, so two feeds pointed at one Worker would each delete the other.
4. Add `<name>` to the `matrix` in `daily.yml`, and to the `feed` dispatch input's options beside it.
5. `curate-validate feeds/` — the half-edited copy is what this catches, and it costs nothing to run.
6. Prove it as in step 5, with the stub scorer, before letting the schedule pay for it.

`curate-validate feeds/` checks the parts of that a person forgets: that no two runners share a state directory, corpus, build directory, Worker or feed URL, that every runner has a subscription list with feeds in it, and that its editorial policy has actually been written. `pytest` checks the one thing left, which is that the matrix and the directories agree.

---

## When something is wrong

| symptom                                                                  | likely cause                                                                                                           |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| Reader shows every item as unread again                                  | `path_prefix` or `site_url` changed after publishing; entry ids are the article URLs, so this means the feed URL moved |
| The run succeeded but nothing was uploaded                               | the feed hash has not moved since the last upload; `curate-deploy --force` overrides that                              |
| The first CI upload asks a question and hangs                            | wrangler is prompting about something a flag did not answer; the job log shows which                                   |
| A job stops at "OPML file not found"                                     | that runner's `sources.opml` was never committed, or its `[paths] sources` points elsewhere                            |
| CI curates feeds you have unsubscribed from                              | the committed `sources.opml` is stale — re-export it                                                                   |
| One feed publishes another's items, or an upload replaces the wrong feed | two runners share a `state_dir`, `build_dir` or `[deploy] project`; `pytest` names which                               |
| A feed stops updating while the others are fine                          | that job failed on its own; `fail-fast` is off, so the run is green overall — read the job, not the run                |
| A shorter feed than usual after a CI outage                              | the Actions cache holding the corpus was evicted; it refills tomorrow, and nothing already read comes back             |
| `curate-score` exits non-zero                                            | the model's answer failed validation twice; the raw text is on disk at `state/<name>/response/DATE.json`               |

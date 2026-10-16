# Setup

Getting your own instance running, with one curated feed.
For every command's options see [CLI.md](CLI.md); for the reasoning, [ARCHITECTURE.md](ARCHITECTURE.md).

You start from the [example configuration repository](https://github.com/ghurault/llm-curated-rss-example), which already holds one working feed, both workflows and a devcontainer.
This guide is what it cannot do for you: your subscriptions, your policy, your URL and your credentials.

**You will need**: a feed reader that exports OPML, Docker and an editor that opens devcontainers (VS Code with _Dev Containers_), a GitHub account, a Cloudflare account (free Workers tier), and from step 7 an Anthropic API account with prepaid credits.

Do the steps in order. All of them can be undone cheaply except step 8: the feed's URL is part of its identity.

---

## 1. Start from the example

Don't fork this repository: it is the engine.
Your feeds go in a **private** repository of your own, because the editorial policy describes what you care about (see [ARCHITECTURE.md](ARCHITECTURE.md), D10).

On the [example](https://github.com/ghurault/llm-curated-rss-example), **Use this template → Create a new repository**, and choose **Private**.
Cloning it works as well, as long as you start a history of your own and push it to a private repository:

```bash
git clone --depth 1 https://github.com/ghurault/llm-curated-rss-example.git my-feeds
rm -rf my-feeds/.git && git -C my-feeds init
```

Nothing in it runs or publishes until you say so: its workflows are dispatch-only and its runner uploads nowhere, which is what the steps below turn on.

## 2. Open it in the devcontainer

The container reads a `.env` file at the repository root, and does not start without one; an empty file is fine.

```bash
touch .env
```

Then _Dev Containers: Reopen in Container_.
The container is the engine's published image, so every `curate-*` command is on `PATH` at the version CI runs.
Check that the copy is sound:

```bash
curate-validate feeds/
```

The engine is pinned twice — the commit in `daily.yml` and the image digest in `.devcontainer/Dockerfile` — and the two move together; the example's README says how under [Upgrading the engine](https://github.com/ghurault/llm-curated-rss-example#upgrading-the-engine).

## 3. Replace the subscriptions

The runner is `feeds/general/`.
Keep that name or change it everywhere it appears — the directory, its `[paths]`, and the matrix and dispatch options in `daily.yml`; this guide calls it `<name>`.

Export OPML from your feed reader over `feeds/<name>/sources.opml` (in Feedly: _Settings → Feeds → Export OPML_), and **commit it**: CI reads the list directly.
While you are in `config.toml`, put your own contact URL in `[fetch] user_agent`, so feed operators have somewhere to complain to.

Run `curate-validate feeds/<name>/config.toml` after every edit.
It needs no network and no credentials.
Every command takes `--config feeds/<name>/config.toml`; nothing defaults to a feed.

## 4. Collect a few days

```bash
curate-fetch --config feeds/<name>/config.toml
```

This appends the lookback window (2 days by default) to `eval/corpus/<name>/YYYY-MM-DD.jsonl` and reports which feeds failed.
Run it daily for a few days, because one day is too little to tune a policy against.
Re-running a day adds nothing.

## 5. Rewrite the editorial policy

The example's `editorial-policy.md` is there to be replaced: its topics, exclusion and penalties describe somebody else's taste.
Nothing validates the policy, and the prompt depends on two conventions, both explained in the comment block of `curate-template policy`:

- a rule's name is its bolded bullet heading;
- the policy says which wins when an exclusion and a topic collide.

The scoring prompt, [`prompt.md`](../src/curated_feed/prompt.md), ships with the engine. To try a different one for a single command, use `--scoring-prompt PATH`.

Score a day **without an API key**, and iterate:

```bash
curate-export --config feeds/<name>/config.toml --prompt > day.md
# paste day.md into a chat session, save the JSON reply as response.json
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider file --response response.json
```

Keep notes in `eval/labels/<name>/` on the right picks, the misses, and what the policy failed to say.
This loop is the point of the project. Step 7 only automates a policy you already trust.

## 6. Prove the feed in your reader

```bash
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --provider stub
```

The stub scorer calls nothing.
Serve `build/<name>/` anywhere your reader can reach (step 9 is one way), subscribe to it, and check two things:

- items appear once, in order;
- they don't come back as unread after a second run.

## 7. Give it an API key

API credits are bought in the [Console](https://console.anthropic.com) and are **separate from a Claude.ai subscription**.
The key comes from the environment, never from the config: put `ANTHROPIC_API_KEY=sk-ant-...` in `.env` and reopen the container, or `export` it in one terminal.

The model drives the cost, and is chosen in `[score]`.
The example starts on Haiku 4.5, which is cheap and rejects the `effort` parameter, so `effort` is left empty there.
The template's default, Sonnet at `effort = "medium"`, is the better choice when the picks matter more than the bill; change both lines together.

Then re-score a day you have labelled, and compare:

```bash
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from score --provider anthropic
```

## 8. Choose the feed's URL

The URL is baked into the feed, so changing it after the first upload makes your reader treat every item as new.

```bash
python3 -c "import secrets; print(secrets.token_hex(4), secrets.token_hex(8))"
```

- The first value is the Worker's name. It is the secret part of the hostname, so it must not be descriptive.
- The second is an optional path segment, which adds a second secret.

No two runners may share either, because an upload replaces a Worker's whole contents.
Replace the example's placeholders, and switch `[deploy] provider` from `"none"`, which is what kept the copy from publishing until now:

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
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from render
```

## 9. The host

The feed is served by a [Cloudflare Worker with static assets](https://developers.cloudflare.com/workers/static-assets/) on the free plan.
**There is nothing to create.** CI's first upload (step 10) creates the Worker under the name from step 8.

To see the feed before CI is set up, go to **Workers & Pages → Create → Upload assets**, use that same name, and upload `build/<name>/`.
Don't use Cloudflare Pages: it mints a new public address on every deployment.

Subscribe to `<site_url>/feed.xml`, or `<site_url>/<path_prefix>/feed.xml`.
To rehearse what CI will run without uploading anything:

```bash
curate-deploy --config feeds/<name>/config.toml --dry-run --provider wrangler
```

## 10. Turn CI on

`daily.yml` owns the schedule, the matrix and the secrets, and calls the engine's [`curate.yml`](../.github/workflows/curate.yml) once per feed.
The corpus and the published log persist in your repository's Actions cache; there is nothing to set up.
Keep its `concurrency` block where it is: a group named inside a called workflow is scoped to the caller.

1. **Add three repository secrets** under **Settings → Secrets and variables → Actions**. Don't put them in an environment: `curate.yml` names none, so they would resolve to empty strings without any warning.

   | secret                  | what it is                                                      |
   | ----------------------- | --------------------------------------------------------------- |
   | `ANTHROPIC_API_KEY`     | the scoring credential                                          |
   | `CLOUDFLARE_API_TOKEN`  | from the **Edit Cloudflare Workers** template, for that account |
   | `CLOUDFLARE_ACCOUNT_ID` | from the dashboard sidebar                                      |

2. **Choose the engine commit.** The `uses:` line pins a full SHA from this repository's `main`, so your daily run changes only when you move it. Keep the example's, or move it and the devcontainer's image together (step 2).
3. **Uncomment the `pull_request:` and `push:` triggers** in `validate.yml`, which checks the runners and the wiring around them.
4. **Dispatch `daily` by hand** (**Actions → daily → Run workflow**), first with `stub` to prove the credentials, then with `anthropic`.

## 11. The schedule

Uncomment the `schedule:` block in `daily.yml` last, once the policy is worth paying for.
It runs at 06:00 UTC, and each run spends credits for every feed.

---

## Adding another feed

1. `curate-template config` and `curate-template policy` into `feeds/<name>/`, replacing every `<name>`, and its own `sources.opml`, committed (steps 3 and 5).
2. A new Worker name and path segment (step 8). Never reuse another feed's.
3. Add `<name>` to the matrix and to the dispatch options in `daily.yml`.
4. `curate-validate feeds/`, which catches runners that share a state directory, corpus, build directory, Worker or URL. It can't see the workflow; `validate.yml` checks the matrix, but not the dispatch options.
5. Prove it with the stub scorer (step 6).

## When something is wrong

| symptom                                     | likely cause                                                                         |
| ------------------------------------------- | ------------------------------------------------------------------------------------ |
| Every item shows as unread again            | `site_url` or `path_prefix` changed after publishing                                 |
| The run succeeded but nothing was uploaded  | the feed has not changed since the last upload; `curate-deploy --force` overrides    |
| The first CI upload hangs                   | wrangler is prompting; the job log shows for what                                    |
| "OPML file not found"                       | `sources.opml` was never committed, or `[paths] sources` points elsewhere            |
| The devcontainer does not start             | there is no `.env` at the repository root                                            |
| CI curates feeds you have unsubscribed from | the committed `sources.opml` is stale; re-export it                                  |
| CI fails on a rejected credential           | the secrets are in an environment, not on the repository                             |
| One feed publishes another's items          | two runners share a `state_dir`, `build_dir` or Worker; `curate-validate` says which |
| One feed stops updating                     | its job failed on its own; read that job's log                                       |
| A shorter feed after a CI outage            | the Actions cache was evicted; it refills, and nothing already read comes back       |
| `curate-score` exits non-zero               | the answer failed validation twice; the raw text is in `state/<name>/response/`      |

## Running it day to day

Once CI runs on its schedule there is nothing to do by hand.
What follows is for when you tune a policy, or something looks wrong.

**Replay a past day rather than refetching it.** Every stage writes its output to a file, so the place to restart from depends on what you changed:

```bash
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from select   # after changing a weight or the item cap
curate-run --config feeds/<name>/config.toml --date 2026-08-08 --from score    # after editing the policy: needs a fresh answer
```

Re-selecting a saved response takes a second and costs nothing.
Either way only that feed is affected: no other runner reads its files or its state.

**When scoring fails.** A response that does not validate is retried once with the error appended.
A second failure publishes nothing and exits non-zero, so the previous feed stays served and a bad day costs one missing day.
A truncated response (`max_tokens`) or a declined request fails the same way rather than producing a short day.
The raw response is kept at `state/<name>/response/DATE.json` either way.

**Only CI publishes.** `curate-run` stops at `build/` unless `--deploy` is given, and the deploy credentials exist only as CI secrets.
`build/<name>/` then holds everything the host serves: `robots.txt`, `_headers`, and `feed.xml` and `index.html` under the path prefix.
An unchanged feed is not re-uploaded; `curate-deploy --force` overrides that, which is needed after changing something the content hash does not cover, such as `_headers`.

**Expect your reader to lag by hours.** The upload takes seconds, but a reader polls a low-traffic feed rarely; Feedly, for one, can take hours.
Refresh the feed by hand in the reader when you want it sooner.

**A late entry looks like a missing one.** An entry carries its article's publication date, not the run time, so with a two-day lookback a fresh entry can arrive already dated two days ago and sort below what you've read.
Check the Actions log and `state/<name>/feed.json` before assuming something didn't publish.

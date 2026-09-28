# Claude Code instructions

The conventions for this repository are in [.github/copilot-instructions.md](.github/copilot-instructions.md).
Read that file and follow it; it is the single source of truth for style, structure and project constraints, whichever assistant is being used.

The rest of this file is the things that are not written down elsewhere and cost time to rediscover.
Nothing here repeats [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (why it is shaped this way), [docs/SETUP.md](docs/SETUP.md) (how to get it running), [CONTRIBUTING.md](CONTRIBUTING.md) (how to work on it) or [README.md](README.md) (how to run it) — read those first.

## Working agreements

- Implementation happens on a branch, never on `main`, in **atomic commits**. One commit per separable decision, with the reasoning in the body rather than a list of touched files.
- **Never rewrite a commit that exists.** No `rebase`, no `--amend`, no `commit --fixup` with autosquash. The branch may already have been pushed, and reconciling a rewritten one costs far more than an untidy history does — the atomicity above is for composing new commits, never a reason to go back. A correction to something already committed is a new commit on top, whatever it touches.
- Ask clarifying questions before building rather than guessing; the user prefers a question to a rework.
- Tests come before the implementation, and never touch the network or a model. Every external dependency in `src/` is injected for exactly that reason — the Anthropic client in `claude.py` (default from `build_client`), the HTTP client in `fetch.py` (`build_client`), the command runner in `deploy.py`. `tests/test_integration.py` substitutes the first two and runs the whole chain; every fixture host is under `.invalid`, so a substitution that fails to take fails the test.
- Flag a deviation from a design document explicitly instead of quietly implementing something better.
- **Comments earn their place**, in configuration files above all. `.github/copilot-instructions.md` asks for concision everywhere, and the failure mode here is a `Dockerfile`, a workflow or a `devcontainer.json` annotated line by line: long enough that none of it gets read. One line for the non-obvious *why*, and nothing for what the line already says.

## Container gotchas

- **Tracked files are owned by `root` while the container runs as `vscode`.** Every edit fails with `EACCES`, and so does `git checkout -b` when `.git/refs/heads/<prefix>/` or `.git/logs/refs/heads/<prefix>/` already exists from an earlier branch. `sudo chown -R vscode:vscode /workspaces/llm-curated-rss` fixes both, and changes nothing but ownership.
- **The devcontainer is built from the repository's own `Dockerfile`**, so a change to it or to `.devcontainer/devcontainer.json` is not real until the container is rebuilt — and rebuilding restarts whatever session is running inside it. `git`, `curl`, `make` and Node are all in the `devcontainer` stage and nowhere else: the image the pipeline runs from carries Node, because `curate-deploy` shells out to `npx wrangler`, and none of the other three.
- **No `prettier` on the command line**, though VS Code formats Markdown, YAML and JSON on save. Markdown tables must therefore be hand-aligned when written, and the editor may silently reformat a file just after a write: a failed `Edit` that reports `ENOENT` or "modified since read" has usually applied anyway. Check with `grep` before retrying, or the same change lands twice.
- After adding a `[project.scripts]` entry point, `pip install -e . --no-deps` before the console script exists.

## Traps in the code

- **There is no root `config.toml`.** Every command takes `--config feeds/<name>/config.toml`, or is run from inside a runner's directory. A bare `curate-run` from the repository root is an error, deliberately: it used to mean "the one feed".
- **A new setting has to reach `src/curated_feed/config.template.toml`.** `tests/test_template.py` compares the template's keys against the settings model, so adding a field to `config.py` without documenting it there fails the suite. That is the point — the template is the reference every new runner is copied from, and it ships with the engine (`curate-template config`) so that the two cannot drift apart across the coming repository split.
- **The saved response is raw text, fence and all.** It is the audit trail for a day that went wrong, so it is written before it is parsed. Anything reading `state/<name>/response/DATE.json` back must go through `score.read_response`, never `state.read_artifact` — models wrap JSON in a Markdown fence often enough that this has already broken selection once.
- **Haiku 4.5 rejects the `effort` parameter.** Each runner names its own model, so this is per config: `feeds/tech` runs Sonnet at `effort = "medium"`, and the empty string means "omit the parameter", not "use the default", so a runner that moves to Haiku 4.5 must blank `effort` in the same edit.
- **`select.py` cannot use bare `assert`** (ruff `S101`); raise `AssertionError` instead. Likewise magic numbers in tests get a named constant rather than a `noqa`, which is the house style.
- **`build_feed` sorts its own entries.** The byte-identical guarantee belongs to the function, not to whoever calls it, so do not "optimise" the sort away by pre-sorting at the call site.

## Deliberate absences

Things that look missing and are not, so nobody helpfully adds them:

- No per-runner overrides file, and no calibration examples — the MVP scores without either.
- No cross-runner deduplication. An article in two runners' source lists is published in both, on purpose: collapsing it would mean one runner's fetch reading another's log (`docs/ARCHITECTURE.md` D8).
- No structured-output schema on the Anthropic call. `docs/ARCHITECTURE.md` records the blocker under Deferred.

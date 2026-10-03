# Contributing

Everything runs inside the devcontainer.
It is a development environment and nothing else: the scheduled run and the upload happen in CI, from the `runtime` stage of the same `Dockerfile`, which is why the `devcontainer` stage is free to carry the test and lint tooling outright.

## Setup

Open the repository in the devcontainer (_Dev Containers: Reopen in Container_) and you are done.
It is built from the repository's own [`Dockerfile`](Dockerfile) — the `devcontainer` stage of the image the scheduled run uses — so creating the container has only the development set, the local package in editable mode and the git hooks left to install.
Editing either that file or `.devcontainer/devcontainer.json` therefore means rebuilding the container before the change is real.

Node is in the image, so `npx wrangler` works for rehearsing an upload.
It is there because the deploy stage shells out to it, not because the pipeline needs a Node toolchain of its own.

## Checks

```bash
pytest
ruff check .
black .
```

Tests use small fixtures in `tests/fixtures/` and **never touch the network** — nor do they call a model.
Adding a test that needs either means the design has gone wrong somewhere; record the response as a fixture instead.

Formatting and linting are the git hooks' job, and [`.github/workflows/quality.yml`](.github/workflows/quality.yml) runs the same hooks on every push and pull request.
It is the same `.pre-commit-config.yaml`, so a commit that got through locally gets through there.

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs `pytest` alongside it, against `requirements.txt` rather than `requirements-dev.txt`.
That is the one check the devcontainer cannot reproduce, because the image installs the development set: a module in `src/` that imports something only a development extra provides passes locally and fails there.

[`.github/workflows/image.yml`](.github/workflows/image.yml) builds the `Dockerfile` and runs a day inside it with the network switched off, and on `main` publishes it to GHCR.
The engine reaches every configuration repository as that image, and every failure mode of that lives in the build rather than in the code, which is why a pull request builds it too.

## Conventions

The full conventions are in [.github/copilot-instructions.md](.github/copilot-instructions.md), which is the single source of truth whichever assistant is being used.
Three of them shape most review comments:

- **Nothing personal in code, defaults, test fixtures or documentation.** This repository is public; feed URLs and topics belong in a configuration repository.
- **Only the policy documents reach the model.** A runner's `editorial-policy.md` is loaded at runtime and `src/curated_feed/prompt.md` ships with the package; `docs/scoring-spec.md` never is.
- **Only CI publishes.** `curate-run` stops at `build/` unless `--deploy` is given, and the devcontainer holds no deploy credential. Tests may exercise the deploy stage but must never run a command: inject the runner, as `tests/test_deploy.py` does.

The version comes from annotated git tags via `setuptools_scm`, following [semantic versioning](https://semver.org/), and commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), enforced by pre-commit.

## Releasing to configuration repositories

Every merge to `main` publishes an image, tagged with its version, `sha-<commit>` and `latest`.
None of that reaches a configuration repository by itself, which is the point (`docs/ARCHITECTURE.md`, D10):

1. Copy the digest from the summary of that `image` run into the `container:` line of [`curate.yml`](.github/workflows/curate.yml), in a pull request of its own.
2. Each configuration repository moves its `uses: …/curate.yml@<commit>` to the commit that merged it, whenever it chooses to.

What `curate.yml` accepts — its inputs, its secret names, the `feeds/<name>/` layout and the cache paths — is what every caller depends on.
Changing any of it is a breaking release, and the cache paths especially: a caller whose paths move loses its corpus and published log without an error.

## Where things are written down

| Document                                        | Holds                                                        | Read it when                    |
| ----------------------------------------------- | ------------------------------------------------------------ | ------------------------------- |
| `README.md`                                     | how to run it                                                | using the thing                 |
| `docs/SETUP.md`                                 | how to get it running the first time                         | setting it up, or re-setting it |
| `docs/ARCHITECTURE.md`                          | why it is shaped this way, the contracts, and the invariants | changing how it works           |
| `docs/scoring-spec.md`                          | the ranking mechanics as implemented                         | changing `select.py`            |
| `src/curated_feed/prompt.md`                    | how the model is told to judge an article                    | changing what the model returns |
| `src/curated_feed/config.template.toml`         | every setting a runner can have, annotated                   | adding or changing a setting    |
| `src/curated_feed/editorial-policy.template.md` | the shape of a policy, and what the prompt relies on in it   | changing how a policy is read   |
| `.github/workflows/curate.yml`                  | what one runner's day does, and what callers depend on       | changing how it is published    |

## Managing requirements

Direct dependencies live in `pyproject.toml`.
Two pinned files are generated from it and should not be edited by hand:

- `requirements.txt` — what the application itself needs. This is what the CI job installs.
- `requirements-dev.txt` — the above plus the development extras, and what creating the devcontainer installs.

After changing a dependency, recompile both and reinstall:

```bash
make reqs && make deps
```

To upgrade everything, add `--upgrade` to the `uv pip compile` commands in the `reqs` target, then `make deps`.

Dependabot proposes upgrades weekly ([`.github/dependabot.yml`](.github/dependabot.yml)), editing the pinned files directly.
That is safe — recompiling prefers what is already written there — but it cannot widen a lower bound in `pyproject.toml`.
An upgrade that needs one is a commit of its own.

## Make commands

| target       | effect                                            |
| ------------ | ------------------------------------------------- |
| `make reqs`  | compile `requirements.txt` from `pyproject.toml`  |
| `make deps`  | install pinned requirements and the local package |
| `make docs`  | build the documentation into `docs/_build/html/`  |
| `make tag`   | create and push the next version tag              |
| `make clean` | delete caches and compiled files                  |
| `make help`  | list all targets                                  |

# Copilot Instructions

## Role & Project Context

You are an expert Python developer.

- Prioritize readability and correctness over cleverness.
- When proposing a solution, clarify the pros and cons of different approaches, and consider the strongest case against your recommendation.
- Be honest, always state when you are unsure, and ask for clarification if needed.
- Unless stated otherwise, be concise in your answers, comments and documentation.

## 1. Code Generation & Style

- **Python Standard**: Default to the Google Python Style Guide.
- **Formatting & Linting**: Assume the codebase is formatted with Black and linted with Ruff. Write code that naturally passes these checks. Key active rule sets include: `B` (bugbear — mutable defaults, etc.), `C4` (comprehensions), `UP` (pyupgrade — f-strings, modern syntax), `SIM` (simplify), `S` (bandit), `PD` / `NPY` (pandas/NumPy idioms, selected ahead of those libraries being used), `N` (naming), `D` (docstrings). See `pyproject.toml` for the full `select` list and exclusions.
- **Type Hints & Pyright**: Write strictly type-valid code designed to pass `pyright` standard checks. However, never make the code compliant to the detriment of readability. Using `# type: ignore` is acceptable in circumstances where strict typing makes the code overwhelmingly complex to read.
- **Docstrings && Comments**: Write Google-style docstrings.
  - For simple, self-explanatory functions, a concise one-line docstring is sufficient. Use full `Args:` and `Returns:` blocks for complex logic where usage isn't immediately obvious from type hints.
  - _Distinction_: Docstrings are for users (explaining **what** it does and **how** to use it). Comments are for developers (explaining **why** a specific implementation choice was made).
  - _Constraint_: Avoid over-commenting. Keep code self-explanatory and reserve comments strictly for non-obvious or tricky logic.
- **Tests**: Write `pytest`-style tests and ensure they pass. Use fixtures for shared test data when possible. When generating code for `src/`, write or suggest `pytest` tests _before_ writing the implementation (test-driven development).
  Tests must not touch the network: use the fixtures in `tests/fixtures/`.
- **Python file structure**
  - _Module Docstrings_: Always include a module-level docstring at the very top of every Python file explaining its purpose.
  - _Interactive Cells_: Use the `# %%` delimiter to separate structural sections in files, followed by a comment in a new line for the title of the section, except for the last section which should not have a title comment.
  - _Line breaks_: Use blank lines in code, sparingly, to indicate logical sections.

## 2. Naming Conventions

- **General**: Use `snake_case` for variables/functions, `PascalCase` for classes, and `UPPER_CASE` for constants. Prefix private variables, functions and classes with an underscore (`_`).
- **Consistency**: Maintain consistent function/variable names and prefixes throughout the codebase.
  - Common short names/prefixes: `n` for counts; `idx` for indices; `rng` for a NumPy Generator (`np.random.default_rng()`); `tmp` or `_tmp` for throwaway variables.
  - Use `is_`, `has_`, or `can_` for boolean-returning/predicate functions or variables (e.g., `is_valid`, `has_converged`). Negated boolean variable names should be avoided (`is_found` rather than `is_not_found`).
  - Constants can be prefixed by a common type name (e.g. `COLOR_RED`, `COLOR_BLUE`).
  - Prefer singular names for single values and plural names for collections (e.g. `user` vs `users`).
    Plural applies to sequences and sets, where the name describes what the iteration yields.
    For mappings, prefer `<value>_by_<key>` (e.g. `feed_by_url`), where the singular/plural rule applies to the value: `feed_by_url` holds one feed per URL, `feeds_by_url` a collection of them.
- **Function Naming**: Prefix function names with verbs that describe their action (e.g., `load_config`, `parse_opml`, `build_record`).

## 3. Structural Principles

- **Avoid Premature Optimization**: Do not over-engineer functions or classes. Prioritize correctness and clarity first.
- **DRY**: Extract duplicated logic into shared utilities.
- **Keep it Small**: Aim to limit functions and classes to under 100 lines. For larger tasks, break them down into well-named, single-responsibility helpers. For example, separate control flow (the act of deciding) from computation (the act of doing).
- **Pure Functions**: Prefer pure functions with no side effects where possible.
  Normalisation, parsing and rendering are pure; I/O and network access are pushed to the edges.
- **SOLID**: Ensure every function/class has a single responsibility, are open for extension but closed for modification, and aim to follow other SOLID principles.

## 4. Python Specifics & Code Quality

- **Doctests**: If a function has a simple return value, embed a doctest in its docstring (mandatory for `src/` code).
- **Paths**: Use `pathlib.Path` for file manipulation.
- **Logging vs. Printing**: Prefer `logging` (e.g., `logging.info()`) to using `print()`.
  Exception: a command's own report is its product and is written to stdout with `print()` — the run summary and the exported corpus listing are output, not diagnostics.
- **EAFP**: Prefer _Easier to Ask for Forgiveness than Permission_: prefer `try`/`except` blocks instead of defensive `if` pre-checks where idiomatic. In particular, avoid returning more than one variable type from a function call (e.g. list or None): if the function is unable to produce the supposed return value it is better to raise an exception that can be caught by the caller instead.
- **Control Flow**: In an `if/else` statement, position the normal or expected execution path within the `if`-clause and reserve the `else`-clause for exceptional cases or anomalies.
- **Data Validation**: Use **Pydantic** for structured data validation and settings management.
- **Assertions**: Use assertion for things that should not happen (if the program is correct), but do not use assertions instead of real error handing (e.g. to validate sensible inputs).
- **Fetching**: One failing feed must never fail a run. Collect failures, report them in the run summary, and be polite to other people's servers: real `User-Agent`, timeouts, bounded concurrency, retries with backoff.

## 5. Function Signatures & Arguments

- **Limit Arguments**: Strongly avoid functions with more than 10 arguments. If exceeded, consider using `**kwargs`, configuration objects, or dependency injection.
- **Keyword-Only Arguments**: Try to limit positional parameters to a maximum of 3. Use the `*` operator to enforce keyword-only arguments for everything else.
  - _Example_: `def build_record(entry, *, feed, fetched_at, summary_max_chars):`
- **Sensible Defaults**: Expose optional configurations via keyword arguments with sensible defaults.
- **No Incompatible Parameters**: Avoid designing functions with mutually exclusive parameters. Split them into separate functions if parameters conflict.

## 6. Project Constraints

These exist because this repository is a public engine, and each reader's configuration lives in a private repository of their own (`docs/ARCHITECTURE.md`, D10).

- **No paths in code**: No path under `feeds/` or `eval/` is hardcoded in `src/`. Locations come from a runner's `config.toml` or from CLI arguments, resolved relative to the config file. This is what makes one instance run several feeds: nothing in `src/` knows there is more than one.
- **Nothing personal**: No feed URLs, topics, employer or personal names in code, defaults, test fixtures or documentation. They belong in a configuration repository. Documentation says `feeds/<name>/` rather than naming a real runner.
- **The record schema is a contract**: the fields, their order and their meaning are consumed by later phases and by manual evaluation sessions. Changing them is a deliberate, discussed act, not a refactor.
- **Only the policy documents reach the model**: a runner's `editorial-policy.md` is loaded at runtime and `src/curated_feed/prompt.md` ships with the package; the two are assembled into the prompt. `docs/scoring-spec.md` never is — the score floor, the item cap and the assembly rules are withheld from the model deliberately, so code that puts them in a prompt breaks the design.
- **The provider is behind a protocol**: scoring goes through `Scorer`. Only `claude.py` imports the Anthropic SDK, and `score.py` imports it lazily; nothing else may assume which provider is configured. The offline backends (`stub`, `file`) stay — they are how the pipeline is tested and how a feed is proved in a reader without spending.
- **Only CI publishes**: uploading goes through `Deployer`, and `curate-run` stops at `build/` unless `--deploy` is given. That flag and the devcontainer's lack of any deploy credential are what stop a hand-run from reaching the internet. Tests may exercise the deploy stage but must never run a command: inject the runner, as `tests/test_deploy.py` does.
- **Credentials come from the environment, never from config**: both the scorer and the deployer resolve their own. A setting that holds a token is a bug, not a convenience.

## 7. Workflow & Tooling

**Pre-commit Hooks**: This repository uses a `.pre-commit-config.yaml` to orchestrate code quality. Key hooks include:

- `typos`: spell-check all text files
- `black`: format Python
- `ruff-isort`: sort imports
- `ruff-docformatter`: fix docstring style
- `ruff-check`: ruff linter
- `make-docs`: regenerate `pdoc` docs when `src` or `README.md` changes
- `conventional-pre-commit` (commit-msg stage): enforce Conventional Commits message format

**Environment**: All commands run inside the devcontainer, never on the host.
Dependencies are declared in `pyproject.toml` and pinned by `make reqs` into `requirements.txt` (the application's own dependencies, which the image installs) and `requirements-dev.txt` (those plus the development extras). Never edit either pinned file by hand.

## 8. Other Languages

Formatters per language are configured in `.vscode/settings.json`; write code so it needs no reformatting once that formatter runs.

- **JSON / JSONC / YAML**: Formatted with Prettier.
- **Markdown**: Formatted with Prettier.
  - _Line breaks_: Never break a line in the middle of a sentence. Prefer to write each sentence (or clause, if long) on its own line.
- **Shell scripts**: Formatted with `shfmt`.
- **TOML**: Formatted with the `even-better-toml` extension (`taplo`).

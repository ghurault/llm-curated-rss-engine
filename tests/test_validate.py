"""Preflight checks on a runner's configuration and the files it names.

The contract under test is what makes one binary serve as a pull-request gate,
an unattended preflight and something run by hand after copying a directory: a
non-zero exit on any problem, and one line per problem naming the runner and the
offending path. Nothing here may touch the network, which is also the rule the
command itself is written to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from curated_feed.config import DeploySettings, PublishSettings
from curated_feed.validate import (
    RESOURCE_READERS,
    check_the_feed_has_an_id,
    check_the_site_url_is_absolute,
    check_the_worker_matches_the_host,
    find_runners,
    main,
    validate_directory,
    validate_runner,
)

CONFIG = """
[paths]
sources = "sources.opml"
policy = "editorial-policy.md"
corpus_dir = "../../corpus/{name}"
state_dir = "../../state/{name}"
build_dir = "../../build/{name}"

[publish]
site_url = "https://{name}-a8b1.example.invalid"

[deploy]
project = "{name}-a8b1"
"""

RUNNER_COUNT = 2
EXIT_USAGE = 2

POLICY = "# Editorial policy\n\nWhat matters.\n"

OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>subscriptions</title></head>
  <body>
    <outline text="A source" type="rss" xmlUrl="https://a.example.invalid/feed.xml"/>
  </body>
</opml>
"""

EMPTY_OPML = OPML.replace(
    '<outline text="A source" type="rss" xmlUrl="https://a.example.invalid/feed.xml"/>',
    "",
)


def make_runner(root: Path, name: str) -> Path:
    """A complete runner: settings, a policy, and a subscription list."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "config.toml").write_text(CONFIG.format(name=name), encoding="utf-8")
    (directory / "editorial-policy.md").write_text(POLICY, encoding="utf-8")
    (directory / "sources.opml").write_text(OPML, encoding="utf-8")
    return directory


def only(problems: list[str]) -> str:
    assert len(problems) == 1, problems
    return problems[0]


@pytest.fixture
def runner(tmp_path: Path) -> Path:
    return make_runner(tmp_path / "feeds", "general")


# %%
# One runner


class TestACompleteRunner:
    def test_it_has_nothing_to_report(self, runner: Path):
        assert validate_runner(runner / "config.toml") == []

    def test_what_it_writes_to_need_not_exist_yet(self, runner: Path):
        """A fresh checkout has none of them, and neither does a container.

        Requiring what a runner writes to would make the command fail exactly
        where it is most useful.
        """
        assert not (runner.parent.parent / "state").exists()

        assert validate_runner(runner / "config.toml") == []


class TestTheEditorialPolicy:
    def test_a_missing_one_is_reported(self, runner: Path):
        (runner / "editorial-policy.md").unlink()

        problem = only(validate_runner(runner / "config.toml"))

        assert "general" in problem
        assert str(runner / "editorial-policy.md") in problem

    def test_an_empty_one_is_reported(self, runner: Path):
        """The state a copied template is left in.

        It reaches the model as an empty half of the prompt.
        """
        (runner / "editorial-policy.md").write_text("   \n\n", encoding="utf-8")

        problem = only(validate_runner(runner / "config.toml"))

        assert str(runner / "editorial-policy.md") in problem


class TestTheSubscriptionList:
    def test_a_missing_one_is_reported(self, runner: Path):
        (runner / "sources.opml").unlink()

        problem = only(validate_runner(runner / "config.toml"))

        assert str(runner / "sources.opml") in problem

    def test_one_that_does_not_parse_is_reported(self, runner: Path):
        (runner / "sources.opml").write_text("<opml><body>", encoding="utf-8")

        problem = only(validate_runner(runner / "config.toml"))

        assert str(runner / "sources.opml") in problem

    def test_one_listing_no_feeds_is_reported(self, runner: Path):
        """Parsing is filesystem work, so it stays inside the no-network rule.

        An OPML that is present but lists nothing fails at fetch otherwise.
        """
        (runner / "sources.opml").write_text(EMPTY_OPML, encoding="utf-8")

        problem = only(validate_runner(runner / "config.toml"))

        assert str(runner / "sources.opml") in problem


class TestSettingsThatWillNotLoad:
    def test_the_reason_is_one_line_naming_the_runner(self, runner: Path):
        (runner / "config.toml").write_text("[paths]\n", encoding="utf-8")

        problem = only(validate_runner(runner / "config.toml"))

        assert problem.startswith("general:")

    def test_a_missing_file_is_reported_rather_than_raised(self, tmp_path: Path):
        problem = only(validate_runner(tmp_path / "nowhere" / "config.toml"))

        assert "nowhere" in problem


# %%
# A directory of runners


@pytest.fixture
def feeds(tmp_path: Path) -> Path:
    root = tmp_path / "feeds"
    make_runner(root, "general")
    make_runner(root, "tech")
    return root


class TestFindingRunners:
    def test_a_runner_is_a_subdirectory_holding_a_config(self, feeds: Path):
        assert [path.name for path in find_runners(feeds)] == ["general", "tech"]

    def test_a_directory_without_one_is_not_a_runner(self, feeds: Path):
        (feeds / "notes").mkdir()

        assert [path.name for path in find_runners(feeds)] == ["general", "tech"]


class TestADirectoryOfRunners:
    def test_a_complete_set_has_nothing_to_report(self, feeds: Path):
        assert validate_directory(feeds) == []

    def test_every_runner_is_checked_and_each_line_names_its_own(self, feeds: Path):
        (feeds / "general" / "editorial-policy.md").unlink()
        (feeds / "tech" / "sources.opml").unlink()

        problems = validate_directory(feeds)

        assert len(problems) == RUNNER_COUNT
        assert problems[0].startswith("general:")
        assert problems[1].startswith("tech:")

    def test_one_runner_that_will_not_load_does_not_hide_the_others(self, feeds: Path):
        """Otherwise the first broken config is the only thing anyone ever sees."""
        (feeds / "general" / "config.toml").write_text(
            "nonsense = 1\n", encoding="utf-8"
        )
        (feeds / "tech" / "sources.opml").unlink()

        problems = validate_directory(feeds)

        assert [problem.split(":")[0] for problem in problems] == ["general", "tech"]

    def test_a_subdirectory_that_is_not_a_runner_is_reported(self, feeds: Path):
        """The half-finished state this command exists to catch.

        A directory copied but not yet given a config is not a runner, and the
        workflow's matrix would not carry it either.
        """
        (feeds / "half-copied").mkdir()

        problem = only(validate_directory(feeds))

        assert "half-copied" in problem

    def test_a_config_at_the_root_is_reported(self, feeds: Path):
        """`find_config_file` searches parent directories.

        One sitting here is the silent fallback for any command run from inside
        a runner-less subdirectory of it.
        """
        (feeds / "config.toml").write_text(
            CONFIG.format(name="stray"), encoding="utf-8"
        )

        problem = only(validate_directory(feeds))

        assert str(feeds / "config.toml") in problem

    def test_a_directory_with_no_runner_at_all_is_a_problem(self, tmp_path: Path):
        empty = tmp_path / "feeds"
        empty.mkdir()

        assert validate_directory(empty)


class TestRunnersShareNothing:
    """The hazard of adding a feed by copying a directory.

    Nothing derives these locations from the runner's name, so a config that was
    copied and half-edited would publish two feeds into one log and upload each
    over the other. Both feeds still render, so nothing reports it.
    """

    @pytest.mark.parametrize("resource", sorted(RESOURCE_READERS))
    def test_a_shared_resource_is_reported(self, feeds: Path, resource: str):
        settings = (feeds / "tech" / "config.toml").read_text(encoding="utf-8")
        (feeds / "tech" / "config.toml").write_text(
            settings.replace("tech", "general"), encoding="utf-8"
        )

        problems = validate_directory(feeds)

        assert any(resource in problem for problem in problems)

    def test_what_neither_runner_has_set_is_not_shared(self, feeds: Path):
        """Two runners that name no Worker are not fighting over one.

        An empty site_url likewise renders the same placeholder id for both,
        without either of them publishing it. Reporting those would make a pair
        of freshly-copied runners look broken; what matters when one of them is
        actually deployed is checked per runner instead.
        """
        for name in ("general", "tech"):
            path = feeds / name / "config.toml"
            path.write_text(
                path.read_text(encoding="utf-8")
                .replace(
                    f'site_url = "https://{name}-a8b1.example.invalid"', 'site_url = ""'
                )
                .replace(f'project = "{name}-a8b1"', 'project = ""'),
                encoding="utf-8",
            )

        assert validate_directory(feeds) == []


# %%
# What the feed claims about itself, against where it is uploaded


def wrangler(project: str) -> DeploySettings:
    return DeploySettings(provider="wrangler", project=project)


class TestTheFeedsIdentity:
    def test_deploying_with_no_site_url_is_reported(self):
        """`feed_url` falls back to a urn when there is no address.

        Every document then advertises a placeholder as the feed's id, and a
        reader keys on that id.
        """
        problem = check_the_feed_has_an_id(
            PublishSettings(site_url=""), wrangler("frog-a8b1")
        )

        assert problem is not None

    def test_a_runner_that_publishes_nowhere_is_left_alone(self):
        """Not deploying is the state a runner is proved in.

        The template ships this way, and so does every runner being tried
        against the stub scorer before it is let near a Worker.
        """
        assert (
            check_the_feed_has_an_id(PublishSettings(site_url=""), DeploySettings())
            is None
        )

    def test_a_site_url_without_a_scheme_is_reported(self):
        """It becomes the feed's id and its self-link verbatim."""
        problem = check_the_site_url_is_absolute(
            PublishSettings(site_url="frog-a8b1.example.invalid")
        )

        assert problem is not None

    def test_an_absolute_one_is_accepted(self):
        assert (
            check_the_site_url_is_absolute(
                PublishSettings(site_url="https://frog-a8b1.example.invalid")
            )
            is None
        )


class TestTheWorkerAndTheHost:
    """Where the bytes go, against what the bytes claim about themselves.

    `deploy.project` decides the first and `publish.site_url` the second.
    Nothing ties them together but a comment in each config, and the edit where
    they drift is rotating the random name after a leak — exactly when a feed
    served at one address and advertising another matters most.
    """

    def test_a_mismatch_is_reported_and_names_both(self):
        problem = check_the_worker_matches_the_host(
            PublishSettings(site_url="https://frog-a8b1.someone.workers.dev"),
            wrangler("frog-a8b2"),
        )

        assert problem is not None
        assert "frog-a8b1" in problem
        assert "frog-a8b2" in problem

    def test_agreement_is_accepted(self):
        assert (
            check_the_worker_matches_the_host(
                PublishSettings(site_url="https://frog-a8b1.someone.workers.dev"),
                wrangler("frog-a8b1"),
            )
            is None
        )

    def test_a_custom_domain_says_nothing_about_the_workers_name(self):
        """`<worker>.<subdomain>.workers.dev` is a convention, not a rule.

        A custom domain breaks the inference entirely.
        """
        assert (
            check_the_worker_matches_the_host(
                PublishSettings(site_url="https://feed.example.invalid"),
                wrangler("frog-a8b1"),
            )
            is None
        )

    def test_another_deployer_says_nothing_either(self):
        assert (
            check_the_worker_matches_the_host(
                PublishSettings(site_url="https://frog-a8b1.someone.workers.dev"),
                DeploySettings(project="frog-a8b2"),
            )
            is None
        )

    def test_it_reaches_a_runner_on_disk(self, runner: Path):
        settings = (runner / "config.toml").read_text(encoding="utf-8")
        (runner / "config.toml").write_text(
            settings.replace(
                'site_url = "https://general-a8b1.example.invalid"',
                'site_url = "https://general-a8b1.someone.workers.dev"',
            ).replace(
                'project = "general-a8b1"',
                'provider = "wrangler"\nproject = "general-a8b2"',
            ),
            encoding="utf-8",
        )

        problem = only(validate_runner(runner / "config.toml"))

        assert problem.startswith("general:")


# %%
# The command


class TestTheCommand:
    def test_a_clean_directory_exits_zero(self, feeds: Path):
        assert main([str(feeds)]) == 0

    def test_a_problem_exits_non_zero_and_prints_one_line_for_it(
        self, feeds: Path, capsys: pytest.CaptureFixture
    ):
        (feeds / "general" / "editorial-policy.md").unlink()
        (feeds / "tech" / "editorial-policy.md").unlink()

        code = main([str(feeds)])

        assert code != 0
        printed = capsys.readouterr().out.splitlines()
        named = sum(line.startswith(("general:", "tech:")) for line in printed)

        assert named == RUNNER_COUNT

    def test_one_config_file_validates_one_runner(self, feeds: Path):
        (feeds / "general" / "editorial-policy.md").unlink()

        assert main([str(feeds / "tech" / "config.toml")]) == 0

    def test_a_path_that_does_not_exist_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        code = main([str(tmp_path / "nowhere")])

        assert code == EXIT_USAGE
        assert capsys.readouterr().err

    def test_a_path_is_required(self):
        """No search of parent directories.

        This repository has no config at its root deliberately, and a
        discovering command would resurrect the "there is one feed" trap under
        a new name.
        """
        with pytest.raises(SystemExit):
            main([])

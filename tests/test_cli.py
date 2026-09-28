"""End-to-end replay: a saved candidate set through to a rendered feed."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from curated_feed.cli import PUBLISHING_STAGE, STAGES, main
from curated_feed.render import ATOM_NAMESPACE

DAY = "2026-08-08"
CANDIDATE_COUNT = 6
STUB_ITEMS = 2
WIDER_STUB_ITEMS = 4

CONFIG = """
[paths]
sources = "sources.opml"
corpus_dir = "corpus"
policy = "editorial-policy.md"
state_dir = "state"
build_dir = "build"

[score]
provider = "stub"
stub_items = {stub_items}

[publish]
window = 100
title = "A feed"
author = "nobody"
site_url = "https://example.invalid"
path_prefix = "a-long-random-segment"
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A complete instance: config, editorial policy and one day of candidates.

    No scoring prompt: that one ships with the package, so an instance cannot
    supply it and does not need to.
    """
    (tmp_path / "config.toml").write_text(
        CONFIG.format(stub_items=STUB_ITEMS), encoding="utf-8"
    )
    (tmp_path / "editorial-policy.md").write_text(
        "# Policy\n\nWhat matters.\n", encoding="utf-8"
    )

    records = [
        {
            "id": f"{number:016d}",
            "url": f"https://example.com/{number}",
            "title": f"Article {number}",
            "summary": "A summary.",
            "source": "A source",
            "source_url": "https://example.com/feed.xml",
            "folder": None,
            "published_at": f"2026-08-08T0{number}:00:00Z",
            "published_missing": False,
            "fetched_at": "2026-08-09T06:00:00Z",
        }
        for number in range(CANDIDATE_COUNT)
    ]
    corpus = tmp_path / "corpus" / f"{DAY}.jsonl"
    corpus.parent.mkdir()
    corpus.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return tmp_path


def replay(workspace: Path, *extra: str) -> int:
    return main(["--config", str(workspace / "config.toml"), "--date", DAY, *extra])


def feed_entries(workspace: Path) -> list[ET.Element]:
    feed = ET.fromstring(  # noqa: S314 - our own output
        (workspace / "build" / "a-long-random-segment" / "feed.xml").read_text(
            encoding="utf-8"
        )
    )
    return feed.findall(f"{{{ATOM_NAMESPACE}}}entry")


class TestReplay:
    def test_a_saved_day_runs_through_to_a_feed(self, workspace: Path):
        assert replay(workspace) == 0

        entries = feed_entries(workspace)
        assert len(entries) == STUB_ITEMS
        assert [entry.find(f"{{{ATOM_NAMESPACE}}}id").text for entry in entries] == [
            "https://example.com/5",
            "https://example.com/4",
        ]

    def test_every_stage_leaves_its_artifact(self, workspace: Path):
        replay(workspace)
        state = workspace / "state"

        assert (state / "response" / f"{DAY}.json").is_file()
        assert (state / "selections" / f"{DAY}.json").is_file()
        assert (state / "published.jsonl").is_file()
        assert (state / "feed.json").is_file()

    def test_fetching_is_skipped_when_a_day_is_named(self, workspace: Path):
        """Nothing reaches the network: there is no subscription list to read."""
        assert not (workspace / "sources.opml").exists()
        assert replay(workspace) == 0

    def test_replaying_the_same_day_changes_nothing(self, workspace: Path):
        replay(workspace)
        feed_path = workspace / "build" / "a-long-random-segment" / "feed.xml"
        first = feed_path.read_text(encoding="utf-8")

        replay(workspace)

        assert feed_path.read_text(encoding="utf-8") == first
        assert (
            len((workspace / "state" / "published.jsonl").read_text().splitlines())
            == STUB_ITEMS
        )

    def test_a_new_policy_changes_the_selection_without_refetching(
        self, workspace: Path
    ):
        replay(workspace)
        config = workspace / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                f"stub_items = {STUB_ITEMS}", f"stub_items = {WIDER_STUB_ITEMS}"
            ),
            encoding="utf-8",
        )

        replay(workspace)

        assert len(feed_entries(workspace)) == WIDER_STUB_ITEMS

    def test_a_broken_stage_stops_the_chain(self, workspace: Path):
        code = replay(workspace, "--provider", "file")

        assert code != 0
        assert not (workspace / "build").exists()


class TestPublishing:
    """Publishing is opt-in, so a replay of a stub day cannot reach the internet."""

    def test_a_plain_run_stops_at_the_build_directory(self, workspace: Path, capsys):
        assert replay(workspace) == 0
        assert "=== deploy ===" not in capsys.readouterr().out

    def test_deploying_is_reached_when_asked_for(self, workspace: Path, capsys):
        assert replay(workspace, "--deploy") == 0
        assert "=== deploy ===" in capsys.readouterr().out

    def test_starting_at_deploy_runs_only_that(self, workspace: Path, capsys):
        """There is nothing after it, so naming it is asking for it."""
        replay(workspace)
        capsys.readouterr()

        assert replay(workspace, "--from", "deploy") == 0
        output = capsys.readouterr().out
        assert "=== deploy ===" in output
        assert "=== render ===" not in output


def stages_recording_into(called: list[str], *, code: int = 0):
    """A stand-in for every stage but publishing, noting which ones are reached.

    Publishing is left out so that a chain which reached it would raise here
    rather than quietly record it. Built from `STAGES` so that adding a stage
    does not silently stop it being covered.
    """

    def record(stage: str):
        def run(_argv: list[str]) -> int:
            called.append(stage)
            return code

        return run

    return {stage: record(stage) for stage in STAGES if stage != PUBLISHING_STAGE}


def test_a_fresh_run_starts_at_fetch(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """With no date given, the chain begins with the network stage."""
    called: list[str] = []
    monkeypatch.setattr(
        "curated_feed.cli.MAIN_BY_STAGE", stages_recording_into(called, code=1)
    )

    main(["--config", str(workspace / "config.toml")])

    assert called == ["fetch"]


def test_a_fresh_run_stops_at_render(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """Publishing is not part of the default chain, so `deploy` is never looked up."""
    called: list[str] = []
    monkeypatch.setattr("curated_feed.cli.MAIN_BY_STAGE", stages_recording_into(called))

    main(["--config", str(workspace / "config.toml")])

    assert called == ["fetch", "score", "paywall", "select", "render"]

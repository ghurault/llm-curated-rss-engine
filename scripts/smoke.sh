#!/bin/bash

# One day of the pipeline, in whatever environment this is run in.
#
# Written for the image. Nothing here is an alternative to the test suite, which
# substitutes the HTTP client and the model and asserts on what came out; the
# question this answers is narrower and cannot be asked from a checkout at all:
# does an *installed* engine work. Three things only break in a wheel — the
# scoring prompt and the two templates are package data, and the console scripts
# are entry points — and each of them is on the path below.
#
# It is offline by construction, which is what lets CI run it with the network
# switched off: the runner it builds scores with the stub, keeps the paywall
# stage off and names no host, so there is nothing to reach and nothing to
# publish. Every URL it writes is under .invalid, as everywhere else here.
#
# Usage: scripts/smoke.sh [DIRECTORY]
#
# DIRECTORY is where the throwaway runner is built, and defaults to a temporary
# one that is deleted afterwards. CI passes a path inside the mounted workspace
# instead, so that a run also answers what a container leaves behind there.

set -euo pipefail

work="${1:-}"
if [ -z "$work" ]; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
else
  rm -rf "$work"
  mkdir -p "$work"
fi

echo "Building a runner in $work"

# From the templates the engine ships, so that this fails when they stop
# travelling in the wheel — which is the failure a checkout can never see.
curate-template config > "$work/config.toml"
curate-template policy > "$work/editorial-policy.md"

# Three edits, and each one is what the template asks a new runner to decide.
# Everything written stays inside this directory, and no host is named: with
# [deploy] provider at "none" there is no way for this to reach the internet
# even if it is handed a token by accident.
sed -i \
  -e 's|^corpus_dir = .*|corpus_dir = "corpus"|' \
  -e 's|^state_dir = .*|state_dir = "state"|' \
  -e 's|^build_dir = .*|build_dir = "build"|' \
  -e 's|^provider = "wrangler"$|provider = "none"|' \
  "$work/config.toml"

cat > "$work/sources.opml" << 'OPML'
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>smoke</title></head>
  <body>
    <outline text="Smoke" type="rss" xmlUrl="https://smoke.example.invalid/feed.xml"/>
  </body>
</opml>
OPML

# A collection day, written by the engine's own writer rather than by a heredoc:
# the corpus record schema is fixed (see models.Candidate) and a fixture that
# spelled it out here would be a second copy of it to keep in step.
python - "$work" << 'PY'
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curated_feed.models import Candidate
from curated_feed.state import append_corpus, corpus_path

ITEMS = 5

work = Path(sys.argv[1])
now = datetime.now(UTC)
append_corpus(
    corpus_path(work / "corpus", now.date()),
    [
        Candidate(
            id=f"{number:016x}",
            url=f"https://smoke.example.invalid/{number}",
            title=f"Article {number}",
            summary="A synthetic candidate, so that a day has something in it.",
            source="Smoke",
            source_url="https://smoke.example.invalid/feed.xml",
            folder=None,
            published_at=now - timedelta(hours=number),
            published_missing=False,
            fetched_at=now,
        )
        for number in range(1, ITEMS + 1)
    ],
)
PY

echo
curate-validate "$work/config.toml"

# From scoring rather than from fetching: the day above is already collected,
# and fetching is the one stage that must talk to somebody else's servers.
echo
curate-run --config "$work/config.toml" --from score --provider stub

feed="$work/build/feed.xml"
if ! grep -q "<entry>" "$feed"; then
  echo "smoke: $feed carries no entries" >&2
  exit 1
fi

echo
echo "Published $(grep -c "<entry>" "$feed") entries to $feed"

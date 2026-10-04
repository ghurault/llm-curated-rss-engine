# Architecture

Decision record. Explains _why_ the system is shaped this way, so that changes are made
deliberately rather than by accident.

For setting one up and running it, see `docs/SETUP.md`; for the commands, `docs/CLI.md`.
For the ranking mechanics, `docs/scoring-spec.md`. For the editorial rules loaded into the
prompt, a runner's `editorial-policy.md`.

---

## Context

A daily job reads a set of RSS feeds, asks an LLM which of the day's articles are worth
reading, and publishes the winners as a small Atom feed. The feed is subscribed in Feedly
**alongside** the original raw subscriptions.

Two facts shape nearly every decision below.

**Nothing can be missed.** The raw feeds remain subscribed, so this is a highlights lane,
not a filter. Recall does not matter; precision does. A wrong pick costs almost nothing,
which is why the system can afford to be aggressive and simple.

**The configuration is private; the engine and the feed are public.** Feedly polls from
its own servers, so the output must be reachable from the open internet. Privacy rests on
an unguessable URL. The editorial policy, the subscriptions and the reading history live in
a private configuration repository and never leave it; this one holds nothing about any
reader (D10).

## Shape

```
fetch    → eval/corpus/FEED/DATE.jsonl        all items in the lookback window
score    → state/FEED/response/DATE.json      raw model response, saved before parsing
paywall  → state/FEED/paywall.jsonl           whether each graded article can be read
select   → state/FEED/selections/DATE.json    validated, scored, ordered
render   → state/FEED/published.jsonl         everything ever published
         → build/FEED/feed.xml                a rolling window over that log
deploy   → external static host               only from CI, only when the hash moved
```

`FEED` is a runner: one directory under `feeds/`, one job in the scheduled workflow,
one Worker (D8). Everything below describes one of them.

---

## D1 — The model judges, code aggregates

This is four separable decisions. Bundling them is the usual mistake.

| Decision                    | Where                            | Why                                                                                                                                                                                                                                                                      |
| --------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The arithmetic              | **Code**                         | Sub-scores on disk make coefficient tuning an offline sweep over the whole corpus, in milliseconds. If the model sums, every weight change costs an API call and returns non-deterministic results, so a change in output cannot be attributed to the change in weights. |
| Threshold and selection     | **Code**                         | A model that knows there are ten slots fills them. An absolute floor applied outside the prompt is what makes a three-item day possible. Follows from the row above: if code scores, code must select.                                                                   |
| Coefficients and the floor  | **Config**                       | Changeable without touching the prompt or the policy document.                                                                                                                                                                                                           |
| Which articles get reported | **Model**, by absolute threshold | Consequence 0 is dropped without further grading. _Not_ a consequence of the rows above — a separate choice, and the one that keeps the response short.                                                                                                                  |

The model supplies only judgements that require reading: consequence, which topics are a
primary subject, whether an exclusion or penalty applies, whether the source is a forum or
aggregator, whether age is beside the point (`durable`, which exempts a retrospective from
the staleness adjustment), duplicate grouping, and one discretionary override per day.
Everything numeric is computed.

What the model is told lives in `src/curated_feed/prompt.md`, which ships with the
engine, and the runner's `editorial-policy.md`, loaded at runtime. `docs/scoring-spec.md` is not sent: it names the floor, the item cap and the
assembly order, which is exactly the knowledge this decision withholds.

### Why not let the model aggregate

The case is real: smaller output, shorter prompt, fewer failure modes, and holistic
judgement that can catch cases where the rubric is simply wrong about an article. **A rubric
is a lossy encoding of taste**, and that loss is the genuine cost of externalising.

The answer is to bound the loss rather than move the arithmetic. The **discretionary flag**
is the escape hatch: one item per day, may bypass the floor and preference, never bypasses a
hard exclusion. That buys holistic override without two sources of truth — two numbers that
can disagree would be a support burden with no consumer.

**What would overturn this:** if unconstrained picks ("which five would you recommend,
ignoring the rubric") consistently beat the computed selection when both are compared
against manual labels, the rubric is destroying information the model has.

## D2 — Deduplication is the model's; code does exact matching only

Code collapses candidates with an identical canonical URL, and nothing else. That happens at
fetch time rather than before the prompt, because the record id _is_ a hash of the canonical
URL: the same article arriving through two feeds is one record, and the second is never
written. No similarity thresholds, no fuzzy matching, no clustering pass.

**Identical normalised titles are not collapsed in code**, though an earlier draft of this
document said they would be. Doing it before the prompt would take those candidates out of
the numbered list, and the coverage invariant below is defined over that list — so a
code-side collapse has to be threaded through scoring, selection and the rejects log to stay
accountable. That is a real amount of machinery for a case the model already handles: two
outlets running the same headline is exactly the grouping it is asked for, and by the
principle below it is a _loud_ failure if it gets it wrong. Revisit if syndicated copies
start taking two slots in the same feed.

Recognising that two differently-titled articles cover the same announcement is a reading
judgement. A title-similarity threshold is wrong in both directions at once, and the model
already has the whole day in a single prompt.

The model **nominates the representative implicitly**, by grading one member of a group and
listing the others as duplicates. Better than a code heuristic such as "longest summary",
which optimises for verbosity rather than substance.

**Grouping must surface rather than stay internal.** Recurrence is a +1 adjustment, so code
needs the independent-outlet count to compute a score at all. A privately-collapsed group
would be invisible aggregation by the back door.

### The principle behind D1 and D2

**Delegate what you would notice.** Three versions of the same announcement in a ten-item
feed is glaring — it would be caught on the first morning, and the feedback loop is free
because the feed is being read anyway. The failure is _loud_, so delegating is low-risk.

Scoring drift is the opposite. A consequence score running systematically half a point
generous produces a feed that looks entirely plausible. It is invisible in the output and
shows up only in score distributions across a corpus. The failure is _silent_, so it wants
externalising.

## D3 — One call for the whole day, no batching

~300 items at ~80 tokens each is roughly 25k tokens of input, comfortably a single request.

Batching would force code-side duplicate clustering, because a model seeing only part of the
day cannot spot a cross-batch duplicate. Avoiding batching therefore removes an entire
module — and the model grouping duplicates across the full day is the better outcome anyway
(D2).

**Accepted risk:** judgement quality may drift across a long generation, with the 250th
record judged less carefully than the 5th. Mitigated by terse records and by dropping most
candidates to a bare index. If picks start looking erratic, diagnose by re-scoring one day
with the candidate order shuffled and comparing per-article scores. Low stability means the
run is too long, and batching plus clustering should return.

## D4 — Stage boundaries are files

Each stage is a pure function from artifacts to artifacts under `state/`. This buys:

- `--replay DATE --from score`, re-running any suffix of the pipeline against saved inputs.
  Policy tuning becomes a seconds-long loop instead of a day per iteration. **This is what
  makes the project maintainable**; without it, every policy tweak has a 24-hour feedback
  cycle.
- Deterministic stages get golden-file tests.
- A crash in rendering never costs an API call.
- The daily diff of `state/` is a readable audit trail of what was selected and why.

## D5 — Rationales are generated in code

The published one-liner is built from structured data, with one deliberate exception below —
never written by the model wholesale. Generating 300 rationales to publish 10 is waste, and it
lengthens the generation in a way that degrades the later records.

Secondary benefit: **privacy-safe by construction.** No free-form text means no risk of the
policy leaking into the public feed, and no denylist to maintain.

The article's own description is published beside the rationale and is not a counterexample:
it is the source's public text carried through unchanged, and nothing in it comes from the
model or from the policy documents.

**What qualifies as structured data is the actual line, not a stand-in for it.** The rationale
reports consequence, the preference count, and any of the fixed adjustments — recurrence,
staleness — that applied. Each of those is either arithmetic this module already does
or one of a handful of constants this module assigns itself (`RECURRENCE`,
`STALENESS` in `select.py`); none of it is text the model chose.

Topics and penalty rule names are the ones that stay out, and for the same reason as each
other: both are unvalidated model output _about the reader's own interests_. Publishing a topic
would put the reader's topic list on a public URL; publishing a penalty's rule name would put
whatever string the model invented for it there instead — the same leak, just arriving through
the adjustments field rather than the topics one. Excluding both is what keeps "nothing about
the reader reaches the feed" a fact about the code rather than a rule to remember. They stay in
the selection log, which is private and where they are more useful anyway.

**`discretionary_reason` is the one exception, and it is deliberate.** Unlike topics or penalty
names, it is not a reflection of the reader's interests: `src/curated_feed/prompt.md` §7 has the model
write it about the article — the angle or the gap in the rules that the ordinary scoring would
have missed — and instructs it never to name the reader, a topic, or a policy rule in doing so.
That keeps the property this section actually cares about intact: nothing published describes
the reader. It does mean this one field is unvalidated free text on a public URL, which the
rest of this section otherwise rules out on principle — flagged here rather than left for
someone to rediscover.

Upgrade path if the phrasing grates: a second small call over the final selection only.
The editorial policy retains the wording rule for that case.

## D6 — Build host and publish host are separate

Actions runs in the private configuration repository and deploys to an external static
host. GitHub Pages on a private repo requires a paid plan, but nothing requires the feed to
be served from GitHub.

This is what lets the sensitive material stay private while the feed stays public. Swapping
the host is one class behind the `Deployer` protocol, alongside the default that publishes
nowhere.

**Publishing is opt-in and gated on a hash.** Two separate decisions, both about a stage
whose failure is invisible — a reader that stops receiving items looks exactly like a quiet
week.

Opt-in, because replaying a saved day is the commonest thing to run by hand and is usually
run against the stub scorer. `curate-run` therefore stops at `build/` unless `--deploy` is
given. The scheduled run is the only thing that publishes, which is also why the
devcontainer image has no deploy credential and needs none.

Gated, because a feed that has not changed should not be re-uploaded: render records the
hash of what it wrote, deploy records the hash of what it sent, and a quiet day costs
nothing. The host's deployment history is then a list of real changes rather than a daily
heartbeat, which is what makes it worth reading when something looks wrong. Only a real
upload writes that hash — a dry run that recorded itself would make the next run skip an
upload that never happened.

**Privacy is enforced at the host, not only at the crawler.** `robots.txt` asks a crawler
not to fetch. `_headers` sets `X-Robots-Tag: noindex, nofollow` on every response, which is
the half that still holds for a crawler that fetches anyway. Both are written to the root of
the build, above the unguessable segment, because that is where a host looks for them.

**The host's name for the deployment is the secret, so it is random.** The feed is served
from `<worker>.<account-subdomain>.workers.dev`, and the account subdomain is shared by
everything ever deployed to that account — so the Worker's own name carries the whole of it,
and a descriptive one would give the feed away. A random `path_prefix` under that hostname
adds a second, independent secret; leaving it empty is a defensible choice rather than a
free one.

**Why a Worker and not Cloudflare Pages**, given that both serve the same files from the
same network for the same nothing. Pages mints a permanent `<hash>.<project>.pages.dev`
address on _every_ deployment, so a daily job accumulates public addresses for something
whose privacy is its URL. `wrangler deploy` updates one address in place. The features that
would argue the other way — Git builds, per-branch previews, dashboard rollbacks — are all
things this project either does not want or already has: `state/published.jsonl` is the real
history, and the feed is rebuilt from it daily.

Reversing this is small, and the reason to record it is that the dashboard leads the other
way: "upload assets" produced a Pages project when D6 was written and produces a Worker now.

## D7 — The provider is a seam, not a dependency

Scoring goes through a protocol. `AnthropicScorer` calls Claude; the stub and file-backed
backends call nothing, and they stay after the real one lands rather than being replaced by
it — the stub is what proves a feed end to end without spending, and the file-backed one is
what lets a chat session drive the real pipeline.

The SDK is imported only when its provider is selected, so a fetch or a render never loads
it. Nothing outside `claude.py` knows which provider is configured.

**What leaves the machine** is the day's headlines and summaries, which are already public,
and the two policy documents. The second is the disclosive one: `editorial-policy.md`
describes what its reader cares about. That is a profiling concern rather than a secrecy
one, and it is the reason the provider is a seam — swapping to a local model, or to a
provider with a different privacy property, is one class.

## D8 — A runner is a directory, and its config is what defines it

`feeds/<name>/` holds one runner: a config, an editorial policy, a subscription
list. The configuration repository's scheduled workflow runs a matrix over those
directories, so a second feed is a directory and a line in that matrix rather than a
second workflow or a fork.

**The config defines the runner; the directory names it.** Everything that makes one
feed differ from another is in that file — what it reads, whose taste it applies,
which model it pays for, which Worker serves it. Identity is the one thing that is
not, because the directory already carries it: a `name` setting would be a second
copy to keep in step, whose only means of staying in step would be a test asserting
it equalled the directory it sat in.

**Nothing in `src/` knows there is more than one.** Paths resolve relative to the
config file, so the partition is entirely in what each config declares. That is why
`state/`, `eval/corpus/` and `build/` gain a subdirectory per runner, rather than
each runner directory gaining a state directory: `feeds/` stays committed whole in
the configuration repository, which makes adding a feed by copying a directory safe —
there is no runtime data to copy by accident.

The price is that two runners _could_ name one state directory, and nothing would
say so: they would append to a single published log and upload each over the other,
and both feeds would still render. Deriving those locations from the directory name
in code would remove the hazard, at the cost of `state_dir` meaning something other
than what it says. It is guarded by `curate-validate` instead.

**One Worker per runner**, because `wrangler deploy --assets` replaces a Worker's
whole asset manifest: two runners uploading to one Worker would each delete the
other's feed. Nothing needs creating at the host for that to be cheap — `wrangler
deploy` creates a Worker that does not exist yet, so a runner's Worker is a random
name in its config and nothing more.

Several feeds _can_ share one Worker — one build tree, distinct path prefixes, one
upload — and the reason not to is what a partial failure does. The upload is the
manifest, so a tree assembled while one feed's job was failing deletes that feed
from the internet, in the one stage whose failure looks exactly like a quiet week
(D6). It also needs a fan-in job holding every feed's output, which leaves the
runners independent only up to the last step.

**Deduplication is per runner.** `existing_ids` reads one corpus directory, so an
article in two runners' source lists is published in both, and a reader subscribed
to both sees it twice. Collapsing across runners would couple them — one runner's
fetch would have to know what another had published — so the answer is to keep the
source lists mostly disjoint and accept the overlap.

**Subscription lists are committed** to the configuration repository. They were kept
out of git while engine and configuration still shared a repository that might one day
be published. The split (D10) put them on the private side, so the reason has expired,
and dropping it ends a copy: CI read each list from a secret that had to be
re-pasted whenever a subscription changed, and a stale one silently curated the
subscriptions of setup day.

**What stays shared:** the code, the credentials, and the scoring prompt — how to
judge an article is generic, and only the editorial policy is a matter of taste.
The prompt ships inside the package rather than sitting beside the runners,
because it is not merely generic but contractual: its response section is the
schema `models.Response` parses and its consequence scale is the range
`models.Graded` validates, so a runner that could point at its own could break
the parser without touching code. `--scoring-prompt PATH` tries a different one
for the length of a command, which is how a prompt change is evaluated.

---

## D9 — Readability is checked in code, and the unreadable is gated rather than penalised

A selected article the reader cannot open is a wasted slot in a feed capped at ten items a
day. Some sources are partly subscriber-only, so a check earns its place — but only for
runners whose sources are, which is why `[paywall] enabled` is off by default and set per
runner.

**The signal is schema.org `isAccessibleForFree`, and it is the only one.** Google requires
it of publishers who want paywalled content indexed, which is why it is on the page and why
it is trustworthy. It is read in one direction: present and false means paywalled, absent
means free. Publishers mark the restriction, never the absence of one.

**This is code, not the model.** Whether a page can be opened is a fact with machine-readable
markup, not a judgement, so D1 puts it here. Putting the page in the prompt would mean tool
use inside the scoring call, which would break three things at once: the single raw response
saved unparsed as the day's audit trail, the cached document prefix `policy.py` builds, and
the one-call-per-day shape of D3. A model reading the page would also catch _unmarked_
paywalls, at the cost of ~50 extra calls a day — and an unmarked paywall is exactly the case
the third verdict declines to guess at.

**The verdict has three values, and `unknown` is not a guess.** `unknown` covers a non-2xx, a
timeout, a body that is not HTML. It is mandatory rather than tidy: some sites serve their
feed happily and refuse their article pages outright, so a two-valued verdict would either
publish nothing from such a source or read a refusal as evidence of a wall, and neither is
true of one.

**Paywalled is a gate, not a penalty.** A -1 adjustment would let a major story the reader
cannot open outscore one they can. An unopenable article is worth nothing whatever it is
about, so it is removed before the floor and before the cap — which is also what lets the
next article take the freed slot instead of leaving a short feed with a qualifying candidate
right behind it. It is recorded under its own reject key, because the coverage check demands
it and because a day filed under `below-floor` would name the wrong cause.

**The stage sits between `score` and `select`.** After scoring, because the response is what
says which candidates are worth a request — the graded records, not the ~150 dropped for
having no substance. Before selection, because the gate has to sit inside the ranking.
Folding the check into `select` would be fewer files, and would also put network I/O into
the one module that is currently pure arithmetic over saved artifacts.

**Verdicts are cached, keyed by candidate id, and never revisited.** This is a cost measure
rather than a correctness one: an article is a candidate on exactly one collection day, and
the published log is append-only, so a verdict that changed between runs could not move a
published entry in or out. What the log buys is that replaying a day and re-running a failed
one are both free, and that nobody's server is asked twice about the same article. The price
is that a paywall which later lifts is never noticed — accepted, because the article's slot
was decided on the day it was selected.

**The stage cannot fail the day.** Total probe failure means every verdict is `unknown`, the
feed renders exactly as it would have, and the exit code stays zero.

This fetches full article pages, and must not be read as delivering the "full-text fetch and
reading-effort scoring" deferred below. It looks at the markup and throws the body away.

### Deliberate absences

- **No body-length heuristic.** There is no cross-site threshold: a truncated article from a
  long-form magazine outruns a complete one from a wire service. Length measures house style,
  not access.
- **No zero-request detection.** The RSS subscriber marker, where it exists at all, is channel
  boilerplate, and nothing survives `build_record` normalisation.
- **No per-site rules.** One marker, uniformly applied. A table of site-specific selectors is
  a maintenance burden that rots silently as sites redesign.
- **No cookies, logins or subscription support.** The reader subscribes to none of the sources,
  so there is no logged-in case to support.
- **No re-probing**, and **no probing of duplicate members** — only the graded representative
  of a group is checked. Both are deferred below.

## D10 — The engine is public, the configuration is private, and an image joins them

Two repositories, because they hold two things with different owners and lifetimes. This
one is the engine: the package, its tests, the scoring prompt and both templates, and
nothing that describes a reader. A configuration repository holds one reader's runners
(`feeds/`), their evaluation notes, the Actions cache carrying the corpus and the
published log, and the workflow that runs them on a schedule. It is private because
`editorial-policy.md` is the most disclosive artifact in the project.

One repository meant one visibility setting for both: the engine could not be published
while a policy shared its history, and a second reader could not run the pipeline without
forking the first reader's taste along with it.

**The engine is consumed as an image pinned by digest, never `latest`.** `image.yml`
publishes from `main`; `curate.yml` pins one digest, and a caller pins `curate.yml` by
commit. An unattended run therefore changes behaviour only when its caller moves that
commit. Before the split, an upgrade was whatever `main` happened to be the next morning.

**The caller owns the clock, the matrix, the secrets and the cache.** `curate.yml` runs one
runner's day and nothing more: a reusable workflow cannot matrix itself, and the cache
belongs to the calling repository, which keeps each reader's corpus in their own. What
`curate.yml` accepts — its inputs, its secret names, the `feeds/<name>/` layout and the cache
paths — is the contract with every caller, so changing any of it is a breaking release.

**What the engine ships is what is contractual.** The scoring prompt and both templates are
in the package (D8), so a configuration repository needs nothing from this one but the
image: `curate-template` prints the references, and `curate-validate` stands in for the test
suite it does not have.

**The engine's history starts at the split.** The alternative was auditing every past
revision for policy content, and no historical commit was worth that.

---

## Contracts

The shapes other things depend on. Changing one is a deliberate act, not a refactor — which
is why they live here rather than in the user documentation.

### Artifacts

Every stage boundary is a file (D4). That is what makes replay possible: a saved day can be
re-scored or re-selected without refetching anything.

```text
eval/corpus/DATE.jsonl     the day's candidates                     (fetch)
state/response/DATE.json   the model's raw answer, saved unparsed   (score)
state/paywall.jsonl        whether each graded article can be read  (paywall)
state/selections/DATE.json what was picked, with the arithmetic     (select)
state/published.jsonl      every entry ever published               (render)
state/feed.json            the hash of what was written             (render)
build/…/feed.xml           the feed itself                          (render)
state/deploy.json          the hash of what was last uploaded       (deploy)
```

Each of those sits under its own runner's directory — `state/FEED/…`,
`eval/corpus/FEED/…`, `build/FEED/…` — because every location comes from that
runner's config and none of them is assembled in code (D8).

`state/selections/DATE.json` is the one to read when tuning. It carries every selected
article's sub-scores and the reason each rejected article was rejected, including near
misses — which is what a policy edit needs, far more than the picks themselves. Its
`anomalies` list records anything the response got wrong that had to be resolved; normally
empty, and a growing one is a policy problem rather than a parsing one.

### The corpus record

One JSON object per line. Later stages and the manual evaluation sessions both consume it.

```json
{
  "id": "9f2a1c4e8b7d0a35",
  "url": "https://example.com/article",
  "title": "Plain-text title",
  "summary": "Plain text, HTML stripped, truncated",
  "source": "Feed title",
  "source_url": "https://example.com/feed.xml",
  "folder": "Folder/Subfolder",
  "published_at": "2026-08-08T09:14:00Z",
  "published_missing": false,
  "fetched_at": "2026-08-09T06:00:00Z"
}
```

- `id` is a truncated SHA-256 of the canonical URL — lowercased host, no fragment, tracking
  parameters (`utm_*`, `fbclid`, …) removed. Feed GUIDs are ignored: they are inconsistent
  across sources and sometimes change between fetches of the same article.
- `published_at` is `null` when the feed gave no usable date, with `published_missing` set.
  Such items are kept, not dropped, and are exempt from the lookback filter — feeds are
  unreliable about dates, and an item with no date is not evidence of an old item.
- `folder` is the folder path from the OPML file, or `null` for a feed filed at the top
  level. Nothing reads it yet; it is there for weighting by source group later.

---

## Invariants

Things that look like tunables but are not. Each fails **silently**.

**The floor equals the maximum consequence score.** Both are 3. This is what lets a
maximum-consequence article matching none of the reader's topics qualify on consequence
alone. Raise the floor above maximum consequence and off-topic items become mathematically
unreachable — the system degrades into a keyword filter with no error anywhere. If either
scale is rescaled, move the floor with it.

**Every candidate is accounted for exactly once**, across the dropped index, the graded
records, and the duplicate lists. This is the _only_ detector of silent omission — a model
losing its place in a long enumeration and finishing cleanly, with articles never
considered. Truncation has its own signal in `stop_reason`; omission does not.

A response that fails this does not cost the day. Omitted articles are recorded under
`unaccounted`, conflicting claims are settled by stated precedence, and every resolution is
listed in the selection's `anomalies`. The invariant is that nothing is lost _silently_;
losing a whole day's feed to one confused index would be the worse failure. The same check
run over the _output_ is a different matter: if selection itself drops or double-counts a
candidate, that is a bug here and it raises.

**Adjustments are ±1.** On a 0–3 scale a ±2 adjustment is two-thirds of the consequence
range and stops being a nudge.

**The model never learns the floor, the caps, or the slot count.** Knowing them could only
tempt it to pre-filter or pad. This covers more than the prompt: a JSON schema sent as a
structured-output format carries Pydantic's field descriptions, which are the docstrings in
`models.py` — and those name the floor and the item cap. Anything that hands a generated
schema to the API must strip `description` first.

**Hard exclusions are gates that nothing bypasses**, including the discretionary pick.
Enforced in code so a model suggestion cannot override one. A paywalled article is one of
them (D9).

**An unreadable verdict must never be a guess.** Only `paywalled` gates; `unknown` and a
candidate with no verdict at all both publish. A detector having a bad afternoon — a timeout,
a site refusing robots, a probe list truncated by `max_checks` — must not be able to shrink
the feed, and the failure would be invisible: a short day looks exactly like a quiet one.

**Entry `<id>` is the canonical original URL, and per-entry `<updated>` is the original
publication date.** Change either and Feedly resurfaces already-read items. The feed-level
`<updated>` is bumped only when a content hash changes.

**An entry's `<summary>` is the source's own description, then the rationale.** Both describe
the article and never the reader, which is what makes publishing them on a public URL safe.
One element rather than one each in `<summary>` and `<content>`, because `<summary>` is the
one every reader is certain to show.

---

## Deferred

Each with the signal that would justify building it.

| Deferred                                                                                             | Trigger                                                                                                                                                                                                                                                                                                                                               |
| ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Code-side clustering, request batching                                                               | Truncation, or score drift on the shuffle-stability check                                                                                                                                                                                                                                                                                             |
| Full-text fetch and reading-effort scoring                                                           | Long articles keep winning slots                                                                                                                                                                                                                                                                                                                      |
| LLM-written rationales                                                                               | Template phrasing proves too mechanical to skim                                                                                                                                                                                                                                                                                                       |
| Topic and source caps                                                                                | An actual monoculture day                                                                                                                                                                                                                                                                                                                             |
| Log-scaled recurrence bonus                                                                          | Linear scaling (§2.3) proves miscalibrated at the tails once enough real `independent_sources` data accumulates in `state/*/response/*.json` to compare against                                                                                                                                                                                       |
| Dropping the recurrence adjustment                                                                   | It correlates with, and can double-count, `consequence` rather than measuring something independent. The downside: it's also a hedge against a single article's consequence being misjudged, via independent corroboration — dropping it loses that check. Revisit if disabling it on `general` doesn't change selections much                        |
| Feedback loop from read/saved signals                                                                | Not possible on Feedly Free; would require moving reader                                                                                                                                                                                                                                                                                              |
| Local model                                                                                          | Only if protecting the policy document becomes the priority                                                                                                                                                                                                                                                                                           |
| Constrained decoding (`output_config.format`)                                                        | Once `Response.dropped` is a list of records rather than a map: free-form keys cannot satisfy the `additionalProperties: false` that structured outputs require, and the map is what makes a dropped article cost 2-3 tokens instead of 20                                                                                                            |
| Durable storage for the corpus and the log                                                           | The Actions cache is evicted often enough to notice. It is deliberately weak storage: losing it shortens a feed rather than resurfacing items, because an entry's id is the article URL                                                                                                                                                               |
| WebSub push instead of waiting for a poll                                                            | Feedly's several-hour poll stops being tolerable. It discloses nothing about the reader, but it puts the URL that is the whole privacy model into a third party's database, and buys hours on a feed that is read once a day                                                                                                                          |
| A custom domain in front of the Worker                                                               | Only if the generated `workers.dev` hostname proves guessable in practice, or the host is swapped for one whose free tier rate-limits its default domain                                                                                                                                                                                              |
| Several feeds from one Worker (D8)                                                                   | Enough runners to want one hostname and one index page; needs a fan-in job that re-renders every feed from its published log before each upload, so that a feed whose job failed is not deleted from the manifest                                                                                                                                     |
| Probing a duplicate group's members, and substituting a readable one for a gated representative (D9) | A day where a gated article had a readable duplicate. Design it **with** the duplicate-language preference in a runner's editorial policy, which is the other substitution within a group — that one is prompt-side and this one would be code-side, and the two can disagree about which member is graded                                            |
| Re-probing a stale verdict (D9)                                                                      | A paywall observably lifting on a source that matters                                                                                                                                                                                                                                                                                                 |
| Model-assisted detection of unmarked paywalls (D9)                                                   | Articles reaching the feed that turn out to be unreadable **and** carry no markup                                                                                                                                                                                                                                                                     |
| Reconciling a language preference with readability (D9)                                              | A runner whose editorial policy prefers one language for a duplicate group, where that language's source is also the one that cannot be checked. The preference then routes picks to the least verifiable source, which is a policy question rather than a detection one — and it needs the duplicate probing above before it can be answered in code |

## Maintaining this document

- Record the decision and the reason, not the implementation. Module lists belong in the
  README, and config and command references in the code (the template, the parsers), where
  they cannot go stale silently.
- When reversing a decision, keep the old entry and note why it changed. The reasoning is
  more valuable than the conclusion.
- Anything in **Invariants** should not be changed without reading its paragraph first.

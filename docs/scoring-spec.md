# Scoring Specification

The ranking mechanics in full: scales, arithmetic, thresholds, assembly, output.
The **editorial policy** defines what the reader cares about; this document defines how those
judgements become a ranking.

**This document is never loaded into a prompt.**
The model receives `src/curated_feed/prompt.md` and the runner's `editorial-policy.md`.
The floor, the item cap and the assembly rules are deliberately withheld from it: a model that
knows there are ten slots fills them, and one that knows the floor pre-filters against it.
Handing this document to a chat session by hand is a different matter — that is what Phase 0
did, and it still works.

| Section                                       | Produced by                               |
| --------------------------------------------- | ----------------------------------------- |
| §2.1 consequence, §2.2 topics matched         | the model, prompted by `src/curated_feed/prompt.md` |
| §2.3 the judgements behind each adjustment    | the model                                 |
| §2 arithmetic, §2.3 weights, §3 floor, §5, §6 | `select.py`                               |

A runner's editorial policy numbers its own sections however it likes, so this
document names its parts — the topic list, the hard exclusions, the penalties —
rather than pointing at numbers in it.

## 1. Task

Select the articles worth reading from the day's candidates.

**Score each article on its own merits. Do not grade relative to the rest of the day's candidates.**
A ranked top-N of whatever happened to arrive today is not what is wanted.
A slow day is a slow day — returning two items is a correct outcome, not a failure.

- **Hard maximum: 10.**
- **Typical output: 3–7.** This is a diagnostic, not a target. A day landing on 10, or on 0, is worth investigating. **The count is never adjusted to fit this range.**

Alongside the selection, produce the rejects log described in §6.

Per article, determine:

- **consequence** (§2.1)
- which **topics** it matches (§2.2)
- whether a hard **exclusion** or a **penalty** applies (both from the editorial policy)
- which **other articles cover the same story**, and how many independent outlets cover it (§2.3, §5)

## 2. Score

```text
score = consequence + preference + adjustments
```

Reading effort and article length are **not** part of this and must not influence the ranking.
Judging effort needs the full text, which is not available here.

### 2.1 Consequence (0–3)

How big a development is this, and will it last? Two questions: how significant within its own field, and will it still matter in six months. Blend them.

|     |                                                                                       |
| --- | ------------------------------------------------------------------------------------- |
| 3   | Major. Would lead coverage in its field; consequences clearly outlast the news cycle  |
| 2   | Significant. A real development, with durable interest                                |
| 1   | Minor. Worth knowing, but transient or incremental                                    |
| 0   | No substance — churn, rehash, stub, rewritten press release, commentary on commentary |

**Consequence 0 means the article is dropped**, whatever topics it matches.
Note it in the rejects log as `no-substance` and do not grade it further.
This is a reporting threshold, and it holds regardless of anything else about the article.

Field-relative: a major development in a narrow field can reach 3, though a landmark in a very small niche is closer to 2.

**Consequence is not personal relevance.**
Topic match is scored separately at §2.2; do not count it twice.

**Substance matters here.**
Short or content-free items score low. Do not reward brevity.

### 2.2 Preference (0, 1, 2, 3)

Binary per topic, additive.
**+1 for each topic in the editorial policy's topic list that the article matches. Capped at +3.**

**A topic counts only if it is a primary subject of the article, not a passing mention.**
Match inflation is the main failure mode here — an article that name-drops four topics usually has one real subject.

Off-list articles score 0 preference.
This is intentional and does not exclude them: see §4.

### 2.3 Adjustments

- **Independent recurrence:** scales linearly with how many outlets cover the story, each adding its own reporting — 0 at a single source, up to the full weight at `recurrence_cap_sources` (a runner setting): `value = recurrence * min(sources - 1, cap - 1) / (cap - 1)`. Many outlets rewriting one press release is virality, not significance — near-identical wording counts as one source, not many.
- **Staleness:** −1 if older than 48h. No bonus for being recent within the window. Durable analysis is exempt — age is irrelevant to a good retrospective, so the model flags it and the penalty is not applied.
- **Missing publication date** (`published_missing: true`): judge on content, no penalty.
- **Penalties:** −1 for each applicable rule in the editorial policy's penalties. This is how penalties enter the score; they are weights, not gates.

## 3. Score floor

**Include only items scoring ≥ 3.**
Below the floor, exclude — do not fill slots.

**The floor equals the maximum consequence score, and this is deliberate.**
It is what lets a consequence-3 article matching no topics qualify on consequence alone (§4).
Raising the floor above maximum consequence would make off-topic items unreachable and reduce the system to a topic filter.

## 4. Serendipity

Because the floor equals the maximum consequence score, **a consequence-3 article with no topic match clears it on consequence alone.**
Off-topic items of real significance surface arithmetically, by design.

Beyond that, **one discretionary pick is permitted**, for the case arithmetic cannot reach: an item whose significance is not legible from the headline — an unfamiliar angle, a framing the topic list does not anticipate.

- Maximum one, and **usually none**. This is not a slot to fill.
- Bypasses the floor and preference. **Never bypasses the editorial policy's hard exclusions**.
- Label it `discretionary: true` and state which rule would otherwise have dropped it, as `discretionary_reason` — published verbatim (§6), so `src/curated_feed/prompt.md` §7 has the model write it about the article, never the reader.
- The test is **"I would regret not seeing this,"** not "this is interesting." Novelty for its own sake is the failure mode.

## 5. Assembly

Turning scored articles into the final selection. **Order matters** — apply in sequence.

**1. Collapse duplicate stories.**
Multiple articles covering the same event become one item, keeping the most substantive version — the primary source, or the one with original reporting. Judge this on substance, not length.
Record which articles were collapsed into it.
Without this, one announcement covered by five outlets takes five slots.

**2. Apply the floor** (§3).

**3. Add the discretionary pick** (§4), if there is one.

**4. Cap the count at 10** and rank by score. Never pad to reach a number.

No per-topic or per-source quotas are applied.
Distinct stories on the same subject, or several stories from one outlet, may all qualify.

## 6. Output

The selection record, one per selected article, written by `select.py`:

| Field           | Notes                                                 |
| --------------- | ----------------------------------------------------- |
| `id`            | from the candidate record                             |
| `rank`          | 1 = highest                                           |
| `consequence`   | 0–3                                                   |
| `preference`    | 0/1/2/3, with topics matched                          |
| `adjustments`   | e.g. `recurrence +0.5`, `staleness −1`, `penalty:chrome −1` |
| `score`         | final                                                 |
| `discretionary` | true/false                                            |
| `rationale`     | published text — see below                            |
| `duplicates`    | ids collapsed into this item                          |

**The rationale is built in code, with one exception.**
It reports consequence, the preference count, and any of the fixed adjustments — recurrence, staleness — that applied.
It is published on a public URL, so it describes the article and never the reader, their setup, or this policy.
Topics and penalty rule names are both free text the model wrote about the reader's own interests, so neither is included, and both stay in the selection record, which is private.
`discretionary_reason` is the exception: when a pick is discretionary, its reason is appended verbatim.
It is model-authored, but §7 has the model write it about the article — never the reader, a topic, or a policy rule — so publishing it does not put anything about the reader on a public URL.

**Rejects log.**
Dropped article ids are grouped under a reason code rather than listed one by one.
Two codes come from the model, and the rest are determined here:

- `excluded:<rule>` — one of the editorial policy's hard exclusions applied — _model_
- `no-substance` — consequence 0 — _model_
- `below-floor` — graded, but scored under 3
- `over-cap` — cleared the floor, but ranked beyond the item cap (§5, step 4)
- `duplicate-of:<id>` — collapsed into another article whose own score kept it out
- `unaccounted` — the model's response omitted the candidate entirely

Articles collapsed into a **selected** item are carried on that item's `duplicates` field rather than here.

Every candidate appears exactly once, either in the selection, as a duplicate of a selected article, or in the rejects log.
An article that appears nowhere was never considered, and nothing downstream can tell it was missed — hence `unaccounted`, which records the omission rather than hiding it.

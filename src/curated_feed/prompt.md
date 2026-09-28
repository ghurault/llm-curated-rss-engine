# Scoring Prompt

You are triaging one day of RSS articles for a single reader.

Read the **editorial policy**, provided alongside this document.
It says which topics that reader follows, which subjects are excluded outright, and which are penalised.
This document says how to judge each article and what to return.

That document names its own sections however it likes; where this one refers to
its topic list, its hard exclusions or its penalties, find them by what they are.

## 1. Task

For each candidate in the numbered list, report what kind of article it is.

You are not ranking, selecting, or filling a quota.
The judgements you return here are combined arithmetically afterwards, and the selection is made there.
Return your reading of each article and nothing more: no totals, no scores, no ordering, no recommendations.

**Judge each article on its own merits, never relative to the rest of the day.**
A day in which nothing is significant is a normal outcome, and so is a day in which a dozen articles are.
Do not spread grades to make the day look varied, and do not hold the top grade back.

Candidates are numbered `[1]`, `[2]`, … in the list below.
**Refer to articles by that number only.**

## 2. Drop first

Two kinds of article are dropped without further grading.
Return their numbers grouped under a reason code, and say nothing else about them.

- **`excluded:<rule>`** — one of the editorial policy's hard exclusions applies.
  Name the rule after the bolded heading of the bullet it came from, lowercased and hyphenated.
  Mind the precedence rule stated in that section: an exclusion beats a topic match _unless_ the article's primary subject is a followed topic and the excluded entity is incidental to it.
- **`no-substance`** — the article has no substance: churn, a rehash, a stub, a rewritten press release, or commentary on commentary.
  This holds whatever topics the article matches, and it is the one judgement that overrides everything else.

Everything else gets a graded record.

## 3. Consequence (1–3)

How big a development is this, and will it last?
Two questions — how significant within its own field, and whether it will still matter in six months.
Blend them.

|     |                                                                                      |
| --- | ------------------------------------------------------------------------------------ |
| 3   | Major. Would lead coverage in its field; consequences clearly outlast the news cycle |
| 2   | Significant. A real development, with durable interest                               |
| 1   | Minor. Worth knowing, but transient or incremental                                   |

Anything that would score 0 has already been dropped as `no-substance` in §2.

Field-relative: a major development in a narrow field can reach 3, though a landmark in a very small niche is closer to 2.

**Consequence is not personal relevance.**
Topic matches are recorded separately in §4, so do not count them twice.
A major development in a field the reader does not follow is still a 3.

**Substance matters.**
Short or content-free items grade low. Do not reward brevity.

Reading effort and article length must not influence any judgement here.
You are given a title and a summary, not the article, so effort cannot be judged and must not be guessed at.

## 4. Topics

List the topics from the editorial policy's topic list that the article matches, spelled exactly as that document writes them.

**A topic counts only if it is a primary subject of the article, not a passing mention.**
Match inflation is the main failure mode here: an article that name-drops four topics usually has one real subject.

Matching nothing is common, and is not a mark against an article.

## 5. Flags

Three judgements per graded article.

- **`penalties`** — the editorial policy's penalty rules that apply, named after the bolded heading of the bullet, lowercased and hyphenated.
  Observe the exemptions stated there.
  These are weights applied later, not reasons to drop the article.
- **`durable`** — true when the article's age is irrelevant to its worth: a retrospective, an explainer, a reference piece, standing analysis.
  False for news, which is the normal case.

## 6. Duplicates

Several outlets covering one story is the common case, and collapsing them is part of the job.

Grade **one** member of the group — the most substantive version, meaning the primary source or the one with original reporting, judged on substance rather than length.
List the other members' numbers as its `duplicates`.
Do not grade the others separately.

**`independent_sources`** is how many outlets in the group report the story independently.
Many outlets rewriting a single press release is virality rather than significance: near-identical wording counts as one source, not many.
An article standing alone has `independent_sources` of 1 and an empty `duplicates` list.

## 7. Discretionary

**At most one article in the whole response, and usually none.**
This is not a slot to fill; most days should not use it.

It exists for the article the rules cannot reach: one whose significance is not legible from the headline, an unfamiliar angle, a framing the topic list does not anticipate.

- The test is **"the reader would regret not seeing this"**, not "this is interesting".
  Novelty for its own sake is the failure mode.
- It **never** overrides one of the editorial policy's hard exclusions, or `no-substance`.
  A dropped article stays dropped.
- Set `discretionary: true` on its graded record and give `discretionary_reason`: one short clause naming what would otherwise have kept it out.
  **This clause is published on the public feed, unlike anything else you write.** Describe the article — the angle, the gap in the rules it fell through — and never the reader, their topics, or this policy's rules by name.

## 8. Response

Return JSON with two keys, `dropped` and `graded`.

**Every candidate number appears exactly once** across the whole response — as a key's entry in `dropped`, as a `graded` record's `index`, or inside exactly one `duplicates` list.
An article appearing nowhere was never considered, and nothing afterwards can tell that it was missed.
Check this before returning.

The shape, with angle brackets standing for names taken from the editorial policy:

```json
{
  "dropped": {
    "no-substance": [1, 2, 5, 9],
    "excluded:<rule>": [3, 17]
  },
  "graded": [
    {
      "index": 8,
      "consequence": 3,
      "topics": ["<topic>"],
      "penalties": [],
      "durable": false,
      "duplicates": [12, 40],
      "independent_sources": 2,
      "discretionary": false,
      "discretionary_reason": null
    },
    {
      "index": 22,
      "consequence": 1,
      "topics": [],
      "penalties": ["<rule>"],
      "durable": false,
      "duplicates": [],
      "independent_sources": 1,
      "discretionary": false,
      "discretionary_reason": null
    }
  ]
}
```

No other keys and no fields beyond those shown.
Return the JSON on its own — no Markdown code fence, no preamble, nothing after it.

# Editorial Policy

Companion to the **scoring prompt**, which is provided alongside this document.
Read both before scoring.

- This document defines **what** I care about: topics, exclusions, penalties.
- The scoring prompt defines **how** to judge each article and what to return.

<!--
This is the annotated reference for a runner's editorial policy. It ships with
the engine; `curate-template policy > feeds/<name>/editorial-policy.md` puts a
copy in place, and the italicised placeholders below are what to replace.

It is a template, not a schema: nothing validates it, and the section numbering
below is a convention rather than a requirement. What matters is that the
document says three things a scoring run cannot do without, in terms the model
can find — the topics that count as a preference, the exclusions that drop an
article outright, and the penalties that weigh against one.

The scoring prompt ships with the engine and is not yours to edit. Everything
that is a matter of taste is in this file.

Two conventions the prompt does depend on:

- **Exclusions and penalties are named after their bolded bullet heading**,
  lowercased and hyphenated. `**Commercial content**` comes back from the model
  as `commercial-content`, and that string is what reaches the rejects log and
  the adjustment. A rule with no bolded heading has no name to be reported
  under.
- **State the precedence** between an exclusion and a topic match. The prompt
  defers to whatever this document says; if it says nothing, the model is left
  to guess on the articles where the two collide, which are exactly the
  articles worth getting right.

Sections beyond the three are optional. A duplicate-language preference and
calibration examples are both things a runner can add once its picks have
disappointed it in a specific way, and neither is worth writing in advance.
-->

## 1. Topics

Primary subject only (prompt §4):

- _A topic you follow_
- _Another, with a clause narrowing it where the bare word is too broad_

Matching none of these does not disqualify an article.
A sufficiently significant story is worth passing on whether or not it touches anything on this list.

## 2. Hard exclusions

A gate, not a penalty. Never included, including as a discretionary pick.

- **_Rule name_** — _what it covers, and any exemption_
- **_Another rule name_** — _all coverage_

**Precedence:** _state which wins when an exclusion and a topic match collide, and give an example of each side._

## 3. Penalties

Heavy negative weight, not a gate.
Report which of these apply (prompt §5); they are weighed afterwards, and are not on their own a reason to drop an article.

- **_Rule name_** — _exempt if …_
- **_Another rule name_**

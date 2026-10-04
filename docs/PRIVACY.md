# Privacy

What each part of the system can see about you.
For the decisions behind it, see [ARCHITECTURE.md](ARCHITECTURE.md) (D5, D7, D10).

Nearly everything below rests on one assumption: **your configuration repository stays private.**

- **Sources** — indirectly reveal your preferences, since subscribing to a feed is itself a signal.
  - Sent to the AI model on every run — see below, this is where most of the risk lies.
  - At rest, stored in your private configuration repository.
  - Per-_published_ article, the source feed's name appears in that entry's `<author>` — the full subscription list is never exposed, only the source of whatever gets selected.
- **Editorial policy** — reveals what you care about directly, and is the most sensitive document in the project.
  - Sent to the AI model on every run.
  - At rest, intended to be stored in your private configuration repository.
  - The published rationale can't leak it: it's built in code from the independent-source count alone, never from model output. Matched topics — the one piece of model output that _would_ leak the policy — are deliberately kept out of the feed and stay in the private selection log instead (D5).
- **Model provider** — sees both documents above, and therefore learns your preferences.
  - Each run sends the day's headlines and summaries, which are already public, the engine's scoring prompt, and the runner's editorial policy.
  - Anthropic does not train on API inputs by default.
  - The provider is a seam, not a hard dependency (D7): swapping to a self-hosted model is one class away, not a rewrite.
- **Publish target** — the feed is public by default, so anyone with the URL could infer your preferences from what's selected.
  - Mitigated at two layers: an unguessable URL, and `robots.txt` plus a `noindex` header so a crawler that fetches anyway still won't index it.
- **RSS reader** — sees the curated feed's URL and content, same as any other subscription.
  - Low incremental risk in the intended use case: since the curated feed is read _alongside_ the raw subscriptions, the reader already sees the same articles and more via those.
- **Source publishers** — see a request for their feed on every run, and nothing more, unless paywall detection is enabled for that runner.
  - With it on, a publisher additionally sees a request for each of their articles the model graded worth considering that day — a much narrower signal than "subscribed to this feed", since it names the individual articles. It is off by default for this reason as well as the traffic.
  - The request carries the configured `User-Agent` and no cookie or credential, so it is not tied to any account with that publisher.

The privacy-maximalist version of this would be a self-hosted LLM with no published feed at all — reading selections from a local file instead.
That's not what's implemented here for convenience, but the architecture doesn't rule it out, and could be investigated in the future.

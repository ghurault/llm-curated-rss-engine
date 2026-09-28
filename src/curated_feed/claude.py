"""The Anthropic-backed scorer: the one place that talks to a model.

Everything the API needs is decided here — the cache split, the token ceiling,
what counts as a failed response — so that `score.py` stays a protocol, a retry
loop and a failure policy, and the rest of the pipeline never learns that a
provider exists.

Three choices are worth knowing about.

**The policy documents are the system prompt, the candidates are the user turn.**
That is the boundary `policy.py` already draws: the documents are byte-identical
every run and are most of the tokens, so they carry the cache breakpoint and are
read back at a tenth of the price. Interpolating anything volatile above them
would silently cost full price on every run.

**Responses stream.** `max_tokens` is deliberately generous — truncation is a
hard failure rather than a short day — and the SDK refuses a non-streaming
request it estimates will outrun the HTTP timeout.

**No `thinking` parameter is sent.** Models differ in how thinking is
configured and which values they accept; the default is the portable choice, and
`effort` (when the configured model supports it) is the knob that matters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import anthropic

from .models import Candidate
from .policy import Prompt
from .score import ScoreError

CACHE_CONTROL = {"type": "ephemeral"}
TRUNCATED = "max_tokens"
REFUSED = "refusal"

RETRY_INSTRUCTION = (
    "Your previous response did not validate: {feedback}\n"
    "Return the whole response again as valid JSON, corrected."
)


@dataclass(slots=True)
class TokenUsage:
    """What a day of scoring cost, in tokens.

    Accumulated across attempts, so a day that needed a retry reports what the
    retry actually cost rather than what the successful call did.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: object) -> None:
        """Fold one response's usage into the running total."""
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def format(self) -> str:
        """One line for the run report.

        >>> TokenUsage(calls=1, input_tokens=2, output_tokens=3).format()
        '1 call, 2 in (0 cached), 3 out'
        """
        plural = "" if self.calls == 1 else "s"
        return (
            f"{self.calls} call{plural}, "
            f"{self.input_tokens} in ({self.cache_read_tokens} cached), "
            f"{self.output_tokens} out"
        )


class AnthropicScorer:
    """Scores a day of candidates with Claude."""

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        effort: str = "",
        timeout_seconds: float | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.usage = TokenUsage()
        self._client = client or build_client(timeout_seconds)

    def generate(
        self,
        candidates: Sequence[Candidate],
        prompt: Prompt,
        *,
        feedback: str | None = None,
    ) -> str:
        """One call: the day's prompt in, the raw response text out."""
        request = self._build_request(prompt, feedback=feedback)
        try:
            with self._client.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ScoreError(
                "the Anthropic API rejected the credentials. Set ANTHROPIC_API_KEY, "
                "or run `ant auth login`; see the README."
            ) from exc
        except anthropic.APIError as exc:
            raise ScoreError(f"the Anthropic API call failed: {_reason(exc)}") from exc

        self.usage.add(message.usage)
        _check_stop_reason(message)
        return _extract_text(message)

    def _build_request(self, prompt: Prompt, *, feedback: str | None) -> dict:
        content = [{"type": "text", "text": prompt.candidates}]
        if feedback is not None:
            content.append(
                {"type": "text", "text": RETRY_INSTRUCTION.format(feedback=feedback)}
            )

        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": prompt.documents,
                    "cache_control": CACHE_CONTROL,
                }
            ],
            "messages": [{"role": "user", "content": content}],
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        return request


def _reason(exc: Exception) -> str:
    r"""What an API error says, plus what it was raised from.

    The SDK turns every exception raised while sending into
    `APIConnectionError`, whose whole message is "Connection error." — so a key
    with a trailing newline, which is illegal in a header and fails before any
    socket is opened, is reported identically to an unreachable host. The
    distinction lives on `__cause__`, and only there.

    >>> _reason(ValueError("Connection error."))
    'Connection error.'
    >>> wrapped = ValueError("Connection error.")
    >>> wrapped.__cause__ = OSError("nodename nor servname provided")
    >>> _reason(wrapped)
    'Connection error. (OSError: nodename nor servname provided)'
    """
    cause = exc.__cause__
    if cause is None or str(cause) in str(exc):
        return str(exc)
    return f"{exc} ({type(cause).__name__}: {cause})"


def build_client(timeout_seconds: float | None) -> anthropic.Anthropic:
    """A client with credentials resolved by the SDK.

    No key is read here on purpose: the SDK also accepts an `ant auth login`
    profile and workload identity, so an unset `ANTHROPIC_API_KEY` does not mean
    there are no credentials. An actual failure surfaces on the first call.

    Public because it is the seam. A scorer built by hand takes its client
    directly, but the one the pipeline builds cannot: `score.make_scorer` reads
    configuration, and a client is not something configuration should hold. This
    factory is therefore what a test substitutes to run the whole chain against
    a fake, and a default nobody can substitute is the thing `deploy.py` already
    learned to avoid.
    """
    if timeout_seconds is None:
        return anthropic.Anthropic()
    return anthropic.Anthropic(timeout=timeout_seconds)


def _check_stop_reason(message: object) -> None:
    """Refuse to treat a cut-short or declined response as a day's judgement."""
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == TRUNCATED:
        raise ScoreError(
            "the response hit max_tokens and is truncated; raise [score] max_tokens. "
            "A truncated response is a hard failure because half a day's judgements "
            "is indistinguishable from a quiet day."
        )
    if stop_reason == REFUSED:
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        raise ScoreError(f"the model declined the request ({category})")


def _extract_text(message: object) -> str:
    """The text blocks, joined. Thinking blocks carry no text and are skipped."""
    blocks = [
        block.text
        for block in getattr(message, "content", [])
        if getattr(block, "type", None) == "text" and block.text
    ]
    if not blocks:
        raise ScoreError("the response contained no text blocks")
    return "".join(blocks)

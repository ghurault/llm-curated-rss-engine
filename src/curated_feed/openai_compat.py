"""The scorer for any endpoint that speaks OpenAI's Chat Completions API.

OpenAI itself, OpenRouter, Ollama, vLLM, a confidential-computing proxy: the
same class, pointed at a different `base_url`. That is why it is written to
what these servers have in common rather than to OpenAI's own API, and four
choices follow from it.

**Chat Completions, not Responses.** It is the API the other servers copy.

**Plain string message content.** The policy documents are the system message
and come first, so a server that caches prompt prefixes can reuse them; nothing
is marked for caching, because there is no portable way to ask.

**The stream is read chunk by chunk** rather than through the SDK's stream
helper, which assumes OpenAI's exact chunk shape. Reasoning that a server
returns in a field of its own (`reasoning_content`) is ignored; reasoning it
returns inline is stripped by `score.parse_response`.

**`max_completion_tokens`, not `max_tokens`.** OpenAI's reasoning models refuse
the older name; a server that predates the newer one may ignore it, in which
case its own default ceiling applies and truncation is still caught by the
finish reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import openai

from .models import Candidate
from .policy import Prompt
from .score import RETRY_INSTRUCTION, ScoreError, TokenUsage, describe_api_error

TRUNCATED = "length"
FILTERED = "content_filter"


@dataclass(slots=True)
class _Reply:
    """One streamed completion, put back together."""

    text_parts: list[str] = field(default_factory=list)
    refusal_parts: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    usage: object | None = None


class OpenAICompatibleScorer:
    """Scores a day of candidates through an OpenAI-compatible endpoint."""

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        effort: str = "",
        base_url: str = "",
        timeout_seconds: float | None = None,
        client: openai.OpenAI | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.usage = TokenUsage()
        self._client = client or build_client(
            base_url=base_url, timeout_seconds=timeout_seconds
        )

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
            with self._client.chat.completions.create(**request) as stream:
                reply = _collect(stream)
        except openai.AuthenticationError as exc:
            raise ScoreError(
                "the endpoint rejected the credentials. Set OPENAI_API_KEY to "
                "the key for [score.openai] base_url; see docs/SETUP.md step 7."
            ) from exc
        except openai.APIError as exc:
            raise ScoreError(
                f"the OpenAI-compatible API call failed: {describe_api_error(exc)}"
            ) from exc

        _record_usage(self.usage, reply.usage)
        _check_reply(reply)
        return "".join(reply.text_parts)

    def _build_request(self, prompt: Prompt, *, feedback: str | None) -> dict:
        user = prompt.candidates
        if feedback is not None:
            user += "\n\n" + RETRY_INSTRUCTION.format(feedback=feedback)

        request = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": prompt.documents},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.effort:
            request["reasoning_effort"] = self.effort
        return request


def build_client(*, base_url: str, timeout_seconds: float | None) -> openai.OpenAI:
    """A client for `base_url`, or for OpenAI when it is empty.

    The key is the SDK's to find, in `OPENAI_API_KEY`. Unlike the Anthropic
    SDK it refuses to build a client without one, so a missing key is reported
    here rather than on the first call. A local server that needs no key still
    needs the variable set, to anything.

    Public because it is the seam a test substitutes, as `claude.build_client`.
    """
    options: dict = {}
    if base_url:
        options["base_url"] = base_url
    if timeout_seconds is not None:
        options["timeout"] = timeout_seconds
    try:
        return openai.OpenAI(**options)
    except openai.OpenAIError as exc:
        raise ScoreError(
            "the openai scorer needs OPENAI_API_KEY to be set, even for a server "
            "that ignores it; see docs/SETUP.md step 7."
        ) from exc


def _collect(chunks: Iterable[object]) -> _Reply:
    """Reassemble a streamed completion from its chunks."""
    reply = _Reply()
    for chunk in chunks:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            reply.usage = usage
        for choice in getattr(chunk, "choices", None) or []:
            if getattr(choice, "index", 0) != 0:
                continue
            delta = getattr(choice, "delta", None)
            if text := getattr(delta, "content", None):
                reply.text_parts.append(text)
            if refusal := getattr(delta, "refusal", None):
                reply.refusal_parts.append(refusal)
            if finish_reason := getattr(choice, "finish_reason", None):
                reply.finish_reason = finish_reason
    return reply


def _record_usage(total: TokenUsage, usage: object | None) -> None:
    """Fold one reply's usage into the total, or count it as unreported."""
    if usage is None:
        total.add_unreported()
        return
    details = getattr(usage, "prompt_tokens_details", None)
    total.add(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
    )


def _check_reply(reply: _Reply) -> None:
    """Refuse to treat a cut-short, declined or empty reply as a day's judgement."""
    if reply.finish_reason == TRUNCATED:
        raise ScoreError(
            "the response hit the token ceiling and is truncated; raise [score] "
            "max_tokens. A truncated response is a hard failure because half a "
            "day's judgements is indistinguishable from a quiet day."
        )
    if reply.refusal_parts:
        raise ScoreError(
            f"the model declined the request: {''.join(reply.refusal_parts)}"
        )
    if reply.finish_reason == FILTERED:
        raise ScoreError("the model declined the request (content filter)")
    if not reply.text_parts:
        raise ScoreError("the response contained no text")

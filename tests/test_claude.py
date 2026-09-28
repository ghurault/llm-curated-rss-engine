"""The Anthropic-backed scorer, exercised against a fake client.

Nothing here reaches the network or needs a key: the scorer takes its client by
injection precisely so that its request shape can be asserted offline. The fake
itself lives in `fakes.py`, because the integration test drives the same one
through the whole chain.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from curated_feed.claude import AnthropicScorer
from curated_feed.policy import Prompt
from curated_feed.score import ScoreError
from fakes import (
    CACHE_READ_TOKENS,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    FakeClient,
    make_message,
)

PROMPT = Prompt(documents="POLICY DOCUMENTS", candidates="[1] A title\n    A summary.")
MODEL = "claude-opus-5"
MAX_TOKENS = 32000


def make_scorer(client: FakeClient, **kwargs) -> AnthropicScorer:
    options = {"model": MODEL, "max_tokens": MAX_TOKENS, "effort": "medium"} | kwargs
    return AnthropicScorer(client=client, **options)


class TestRequest:
    def test_the_policy_documents_are_the_cacheable_system_prompt(self):
        """They are identical every run and are most of the tokens."""
        client = FakeClient()
        make_scorer(client).generate([], PROMPT)
        system = client.requests[0]["system"]

        assert system[0]["text"] == PROMPT.documents
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_the_candidates_are_the_user_turn(self):
        """The half that changes daily sits after the cached prefix."""
        client = FakeClient()
        make_scorer(client).generate([], PROMPT)
        request = client.requests[0]

        assert PROMPT.candidates not in request["system"][0]["text"]
        assert request["messages"][0]["role"] == "user"
        assert PROMPT.candidates in request["messages"][0]["content"][0]["text"]

    def test_the_model_and_token_ceiling_come_from_configuration(self):
        client = FakeClient()
        make_scorer(client).generate([], PROMPT)

        assert client.requests[0]["model"] == MODEL
        assert client.requests[0]["max_tokens"] == MAX_TOKENS

    def test_effort_is_sent_when_configured(self):
        client = FakeClient()
        make_scorer(client, effort="high").generate([], PROMPT)

        assert client.requests[0]["output_config"] == {"effort": "high"}

    def test_effort_is_omitted_when_blank(self):
        """Not every model accepts it; blank means 'do not send the parameter'."""
        client = FakeClient()
        make_scorer(client, effort="").generate([], PROMPT)

        assert "output_config" not in client.requests[0]

    def test_the_retry_carries_the_validation_error(self):
        client = FakeClient()
        make_scorer(client).generate([], PROMPT, feedback="graded.0.index: too small")
        content = client.requests[0]["messages"][0]["content"]

        assert len(content) == 2  # noqa: PLR2004 - candidates, then the correction
        assert "too small" in content[1]["text"]

    def test_no_thinking_parameter_is_sent(self):
        """Models differ on how thinking is configured; the default is portable."""
        client = FakeClient()
        make_scorer(client).generate([], PROMPT)

        assert "thinking" not in client.requests[0]


class TestResponse:
    def test_only_text_blocks_are_returned(self):
        """Adaptive thinking is on by default, so a thinking block arrives too."""
        client = FakeClient(make_message('{"graded": []}'))

        assert make_scorer(client).generate([], PROMPT) == '{"graded": []}'

    def test_a_truncated_response_is_a_hard_failure(self):
        """Half a day's judgements looks like a valid short day. It is not."""
        client = FakeClient(make_message(stop_reason="max_tokens"))

        with pytest.raises(ScoreError, match="max_tokens"):
            make_scorer(client).generate([], PROMPT)

    def test_a_refusal_is_reported_with_its_category(self):
        client = FakeClient(
            make_message(
                stop_reason="refusal",
                stop_details=SimpleNamespace(category="cyber", explanation=None),
            )
        )

        with pytest.raises(ScoreError, match="cyber"):
            make_scorer(client).generate([], PROMPT)

    def test_an_empty_response_is_reported(self):
        client = FakeClient(make_message(""))

        with pytest.raises(ScoreError, match="no text"):
            make_scorer(client).generate([], PROMPT)


class TestFailures:
    def test_a_missing_credential_says_so(self):
        """The commonest first-run failure deserves better than a stack trace."""

        class Unauthorised(FakeClient):
            def _stream(self, **kwargs):
                raise anthropic.AuthenticationError(
                    "unauthorised",
                    response=httpx.Response(401, request=httpx.Request("POST", "/")),
                    body=None,
                )

        with pytest.raises(ScoreError, match="ANTHROPIC_API_KEY"):
            make_scorer(Unauthorised()).generate([], PROMPT)

    def test_a_connection_failure_says_what_went_wrong_underneath(self):
        """`APIConnectionError` stringifies to "Connection error." and nothing else.

        Every local failure reaches it — a malformed header as readily as an
        unreachable host — so the cause is the whole of the diagnosis.
        """

        class Unreachable(FakeClient):
            def _stream(self, **kwargs):
                raise anthropic.APIConnectionError(
                    request=httpx.Request("POST", "/")
                ) from ValueError("illegal character in header value")

        with pytest.raises(ScoreError, match="illegal character in header value"):
            make_scorer(Unreachable()).generate([], PROMPT)


class TestUsage:
    def test_tokens_are_recorded(self):
        client = FakeClient()
        scorer = make_scorer(client)
        scorer.generate([], PROMPT)

        assert scorer.usage.input_tokens == INPUT_TOKENS
        assert scorer.usage.output_tokens == OUTPUT_TOKENS
        assert scorer.usage.cache_read_tokens == CACHE_READ_TOKENS

    def test_a_retry_adds_to_the_total(self):
        """A day that took two attempts cost two attempts."""
        client = FakeClient()
        scorer = make_scorer(client)
        scorer.generate([], PROMPT)
        scorer.generate([], PROMPT, feedback="invalid")

        assert scorer.usage.output_tokens == 2 * OUTPUT_TOKENS
        assert scorer.usage.calls == 2  # noqa: PLR2004 - one attempt, then the retry

    def test_a_failed_attempt_still_counts(self):
        """Truncation is billed like any other response."""
        client = FakeClient(make_message(stop_reason="max_tokens"))
        scorer = make_scorer(client)
        with pytest.raises(ScoreError):
            scorer.generate([], PROMPT)

        assert scorer.usage.output_tokens == OUTPUT_TOKENS

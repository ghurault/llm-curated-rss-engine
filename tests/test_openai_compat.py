"""The OpenAI-compatible scorer, exercised against a fake client.

The request is held to what the servers that copy OpenAI's API have in common:
Chat Completions, plain string message content, and no parameter that is only
sent because OpenAI itself would accept it.
"""

from __future__ import annotations

import httpx2
import openai
import pytest

from curated_feed.openai_compat import OpenAICompatibleScorer, build_client
from curated_feed.policy import Prompt
from curated_feed.score import ScoreError
from fakes import (
    CHAT_CACHED_TOKENS,
    CHAT_COMPLETION_TOKENS,
    CHAT_PROMPT_TOKENS,
    FakeChatClient,
    make_chunks,
)

PROMPT = Prompt(documents="POLICY DOCUMENTS", candidates="[1] A title\n    A summary.")
MODEL = "an-open-model"
MAX_TOKENS = 32000
ATTEMPTS_WITH_RETRY = 2


def make_scorer(client: FakeChatClient, **kwargs) -> OpenAICompatibleScorer:
    options = {"model": MODEL, "max_tokens": MAX_TOKENS} | kwargs
    return OpenAICompatibleScorer(client=client, **options)


def sent(client: FakeChatClient, index: int = 0) -> dict:
    return client.requests[index]


class TestRequest:
    def test_the_policy_documents_are_the_system_message_and_come_first(self):
        """Prefix caching, where a server has it, needs the constant half first."""
        client = FakeChatClient()
        make_scorer(client).generate([], PROMPT)
        system, _ = sent(client)["messages"]

        assert system == {"role": "system", "content": PROMPT.documents}

    def test_the_candidates_are_the_user_message(self):
        client = FakeChatClient()
        make_scorer(client).generate([], PROMPT)
        _, user = sent(client)["messages"]

        assert user == {"role": "user", "content": PROMPT.candidates}

    def test_the_model_and_token_ceiling_come_from_configuration(self):
        """`max_tokens` is refused by OpenAI's reasoning models; this name is not."""
        client = FakeChatClient()
        make_scorer(client).generate([], PROMPT)

        assert sent(client)["model"] == MODEL
        assert sent(client)["max_completion_tokens"] == MAX_TOKENS
        assert "max_tokens" not in sent(client)

    def test_the_response_streams_and_asks_for_its_usage(self):
        """A stream reports usage only when asked to, in a final chunk."""
        client = FakeChatClient()
        make_scorer(client).generate([], PROMPT)

        assert sent(client)["stream"] is True
        assert sent(client)["stream_options"] == {"include_usage": True}

    def test_effort_is_sent_as_reasoning_effort_when_configured(self):
        client = FakeChatClient()
        make_scorer(client, effort="high").generate([], PROMPT)

        assert sent(client)["reasoning_effort"] == "high"

    def test_effort_is_omitted_when_blank(self):
        """Not every server or model accepts it; blank means 'do not send it'."""
        client = FakeChatClient()
        make_scorer(client, effort="").generate([], PROMPT)

        assert "reasoning_effort" not in sent(client)

    def test_the_retry_carries_the_validation_error(self):
        client = FakeChatClient()
        make_scorer(client).generate([], PROMPT, feedback="graded.0.index: too small")
        _, user = sent(client)["messages"]

        assert user["content"].startswith(PROMPT.candidates)
        assert "too small" in user["content"]


class TestResponse:
    def test_the_streamed_text_is_reassembled(self):
        """Reasoning deltas carry no content and are skipped."""
        client = FakeChatClient(make_chunks('{"graded": []}'))

        assert make_scorer(client).generate([], PROMPT) == '{"graded": []}'

    def test_a_truncated_response_is_a_hard_failure(self):
        """Half a day's judgements looks like a valid short day. It is not."""
        client = FakeChatClient(make_chunks(finish_reason="length"))

        with pytest.raises(ScoreError, match="max_tokens"):
            make_scorer(client).generate([], PROMPT)

    def test_a_filtered_response_is_reported_as_declined(self):
        client = FakeChatClient(make_chunks(finish_reason="content_filter"))

        with pytest.raises(ScoreError, match="declined"):
            make_scorer(client).generate([], PROMPT)

    def test_a_refusal_is_reported_with_its_reason(self):
        client = FakeChatClient(make_chunks("", refusal="I cannot help with that."))

        with pytest.raises(ScoreError, match="cannot help"):
            make_scorer(client).generate([], PROMPT)

    def test_an_empty_response_is_reported(self):
        client = FakeChatClient(make_chunks(""))

        with pytest.raises(ScoreError, match="no text"):
            make_scorer(client).generate([], PROMPT)


class TestFailures:
    def test_a_rejected_credential_says_which_variable(self):
        class Unauthorised(FakeChatClient):
            def _create(self, **kwargs):
                raise openai.AuthenticationError(
                    "unauthorised",
                    response=httpx2.Response(401, request=httpx2.Request("POST", "/")),
                    body=None,
                )

        with pytest.raises(ScoreError, match="OPENAI_API_KEY"):
            make_scorer(Unauthorised()).generate([], PROMPT)

    def test_a_connection_failure_says_what_went_wrong_underneath(self):
        class Unreachable(FakeChatClient):
            def _create(self, **kwargs):
                raise openai.APIConnectionError(
                    request=httpx2.Request("POST", "/")
                ) from ValueError("illegal character in header value")

        with pytest.raises(ScoreError, match="illegal character in header value"):
            make_scorer(Unreachable()).generate([], PROMPT)


class TestClient:
    def test_a_missing_key_is_reported_before_any_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The SDK refuses to build a client without one, even for a local server."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(ScoreError, match="OPENAI_API_KEY"):
            build_client(base_url="", timeout_seconds=None)

    def test_the_configured_endpoint_is_used(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
        client = build_client(base_url="http://localhost:8080/v1", timeout_seconds=None)

        assert str(client.base_url) == "http://localhost:8080/v1/"

    def test_an_empty_endpoint_leaves_the_sdk_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        client = build_client(base_url="", timeout_seconds=None)

        assert client.base_url.host == "api.openai.com"


class TestUsage:
    def test_tokens_are_recorded(self):
        scorer = make_scorer(FakeChatClient())
        scorer.generate([], PROMPT)

        assert scorer.usage.input_tokens == CHAT_PROMPT_TOKENS
        assert scorer.usage.output_tokens == CHAT_COMPLETION_TOKENS
        assert scorer.usage.cache_read_tokens == CHAT_CACHED_TOKENS

    def test_a_retry_adds_to_the_total(self):
        scorer = make_scorer(FakeChatClient())
        scorer.generate([], PROMPT)
        scorer.generate([], PROMPT, feedback="invalid")

        assert scorer.usage.calls == ATTEMPTS_WITH_RETRY
        assert scorer.usage.output_tokens == ATTEMPTS_WITH_RETRY * (
            CHAT_COMPLETION_TOKENS
        )

    def test_usage_a_server_did_not_report_is_not_reported_as_zero(self):
        """Not every server honours `include_usage`; an empty count would lie."""
        scorer = make_scorer(FakeChatClient(make_chunks(is_usage_reported=False)))
        scorer.generate([], PROMPT)

        assert scorer.usage.calls == 1
        assert scorer.usage.unreported_calls == 1
        assert "not reported" in scorer.usage.format()

    def test_a_failed_attempt_still_counts(self):
        scorer = make_scorer(FakeChatClient(make_chunks(finish_reason="length")))
        with pytest.raises(ScoreError):
            scorer.generate([], PROMPT)

        assert scorer.usage.output_tokens == CHAT_COMPLETION_TOKENS

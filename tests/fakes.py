"""A stand-in for the Anthropic client, shared by the tests that need one.

The scorer takes its client by injection precisely so that no test has to reach
the network or hold a key. Two of them need the same fake — the unit tests that
assert the request shape, and the integration test that drives the whole chain
through `curate-run` — so it lives here rather than in either of them.

What the SDK returns is duck-typed rather than constructed: `Message` and its
content blocks are pydantic models with a great deal more on them than the
scorer reads, and building real ones would assert the SDK's shape instead of
ours.
"""

from __future__ import annotations

from types import SimpleNamespace

EMPTY_RESPONSE = '{"dropped": {}, "graded": []}'

INPUT_TOKENS = 1200
OUTPUT_TOKENS = 340
CACHE_READ_TOKENS = 900


def make_message(
    text: str = EMPTY_RESPONSE,
    *,
    stop_reason: str = "end_turn",
    stop_details=None,
):
    """A stand-in for the SDK's Message, carrying only what the scorer reads."""
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            cache_read_input_tokens=CACHE_READ_TOKENS,
            cache_creation_input_tokens=0,
        ),
    )


class FakeClient:
    """Records the request and returns a canned message.

    Several messages can be queued, which is how a retry is exercised: the first
    reply is the one that fails validation and the second is the correction. The
    last one queued is repeated if the caller asks again, so a client given one
    reply answers every call with it.
    """

    def __init__(self, *messages):
        self.messages = SimpleNamespace(stream=self._stream)
        self.replies = list(messages) or [make_message()]
        self.requests: list[dict] = []

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return _FakeStream(reply)


class _FakeStream:
    """The context manager `messages.stream` returns."""

    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self.message

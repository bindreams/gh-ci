from __future__ import annotations

from typing import Any

import pytest


class FakeGh:
    """A controllable fake for gh_api_get and gh_api_graphql."""

    def __init__(self) -> None:
        # Each path can map to either a single response (returned each call)
        # or a list of responses (popped in order).
        self.get_responses: dict[str, Any] = {}
        self.graphql_responses: list[dict] = []
        self.get_calls: list[dict] = []
        self.graphql_calls: list[dict] = []

    # Configuration =====

    def set_get(self, path: str, response: Any) -> None:
        """Set a single response for path; reused for every call."""
        self.get_responses[path] = response

    def queue_get(self, path: str, *responses: Any) -> None:
        """Queue a sequence of responses for path."""
        self.get_responses[path] = list(responses)

    def queue_graphql(self, *responses: dict) -> None:
        self.graphql_responses.extend(responses)

    # gh_api_get drop-in =====

    def gh_get(self, path: str, *, host: str | None = None, paginate: bool = False):
        self.get_calls.append({"path": path, "host": host, "paginate": paginate})
        # Strip query string for path matching.
        base = path.split("?", 1)[0]
        for key in (path, base):
            if key in self.get_responses:
                resp = self.get_responses[key]
                if isinstance(resp, list):
                    return resp.pop(0) if resp else {}
                return resp
        raise AssertionError(f"FakeGh: no response set for {path!r}")

    # gh_api_graphql drop-in =====

    def gh_graphql(self, query: str, *, host: str | None = None, **variables):
        self.graphql_calls.append({"host": host, "variables": variables})
        if not self.graphql_responses:
            raise AssertionError("FakeGh: no graphql responses queued")
        return self.graphql_responses.pop(0)


@pytest.fixture
def fake_gh() -> FakeGh:
    return FakeGh()

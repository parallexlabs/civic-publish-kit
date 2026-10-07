"""Review finding M4 regression: discarded streaming attempts still consume the source budget [17]."""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
import requests
from urllib3.response import HTTPResponse

from civic_publish_kit.acquire import AcquisitionError, fetch_layer
from civic_publish_kit.config import Limits

HOST = "services5.arcgis.com"
LAYER = "https://services5.arcgis.com/test_only/arcgis/rest/services/TestOnly/FeatureServer/0"


def _stream_response(
    patch: pytest.MonkeyPatch,
    error: Exception,
    consumed: list[bytes],
    *,
    declared: str | None = None,
) -> tuple[requests.Response, HTTPResponse]:
    raw = HTTPResponse(body=io.BytesIO(b"abcdef"), preload_content=False)
    response = requests.Response()
    response.status_code = 200
    response.raw = raw
    if declared is None:
        response.headers["Transfer-Encoding"] = "chunked"
    else:
        response.headers["Content-Length"] = declared

    def chunks(_chunk_size: int = 1, _decode_unicode: bool = False) -> Iterator[bytes]:
        consumed.append(b"abcdef")
        yield b"abcdef"
        raise error

    patch.setattr(response, "iter_content", chunks)
    return response, raw


@pytest.mark.parametrize("budget", [10, 12])
@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("TEST-ONLY interrupted connection"),
        requests.Timeout("TEST-ONLY interrupted read"),
        requests.exceptions.ChunkedEncodingError("TEST-ONLY interrupted chunk"),
    ],
)
def test_failed_stream_bytes_are_charged_and_exhaustion_stops_before_third_attempt(
    monkeypatch: pytest.MonkeyPatch, budget: int, error: Exception,
) -> None:
    sent: list[requests.PreparedRequest] = []
    consumed: list[bytes] = []
    raw_responses: list[HTTPResponse] = []
    sleeps: list[float] = []
    monkeypatch.setattr("civic_publish_kit.acquire._sleep", sleeps.append)
    limits = replace(Limits(), max_response_bytes=budget, max_retries=2)
    with requests.Session() as session:
        def send(request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
            sent.append(request)
            if len(sent) > 2:
                pytest.fail("A third attempt escaped the aggregate byte budget")
            assert request.method == "GET"
            assert kwargs["timeout"] == (20.0, 20.0)
            assert kwargs["allow_redirects"] is False
            assert kwargs["stream"] is True
            response, raw = _stream_response(monkeypatch, error, consumed)
            raw_responses.append(raw)
            return response

        monkeypatch.setattr(session, "send", send)
        with pytest.raises(AcquisitionError, match="byte"):
            fetch_layer(LAYER, "1=1", ("PROJECT_NO",), (HOST,), limits, session=session)

    assert consumed == [b"abcdef", b"abcdef"]
    assert sum(map(len, consumed)) == 12
    assert len(sent) == 2
    assert sent[0].url == sent[1].url
    assert sleeps == [0.5]
    assert [raw.closed for raw in raw_responses] == [True, True]


def test_content_length_uses_budget_remaining_after_discarded_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[requests.PreparedRequest] = []
    consumed: list[bytes] = []
    raw_responses: list[HTTPResponse] = []
    sleeps: list[float] = []
    monkeypatch.setattr("civic_publish_kit.acquire._sleep", sleeps.append)
    limits = replace(Limits(), max_response_bytes=10, max_retries=2)
    with requests.Session() as session:
        def send(request: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:
            sent.append(request)
            if len(sent) > 2:
                pytest.fail("A third request followed fatal Content-Length exhaustion")
            declared = None if len(sent) == 1 else "6"
            response, raw = _stream_response(
                monkeypatch,
                requests.exceptions.ChunkedEncodingError("TEST-ONLY interrupted chunk"),
                consumed,
                declared=declared,
            )
            raw_responses.append(raw)
            return response

        monkeypatch.setattr(session, "send", send)
        with pytest.raises(AcquisitionError, match="response would exceed per-source byte limit"):
            fetch_layer(LAYER, "1=1", ("PROJECT_NO",), (HOST,), limits, session=session)

    assert consumed == [b"abcdef"]
    assert len(sent) == 2
    assert sleeps == [0.5]
    assert [raw.closed for raw in raw_responses] == [True, True]

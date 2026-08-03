from __future__ import annotations

import json

import httpx
import pytest

from flyboard.model_calls import (
    CallPurpose,
    HttpxModelTransport,
    ImageContentPart,
    ModelCallPolicy,
    ModelCallRequest,
    ModelEndpointConfig,
    ModelMessage,
    TextContentPart,
    TransportFailure,
    TransportFailureReason,
)


def policy() -> ModelCallPolicy:
    return ModelCallPolicy(
        policy_version="model-call-policy-v1",
        call_purpose=CallPurpose.XLSX,
        model_id="qwen3.7-plus-2026-05-26",
        temperature=0.0,
        enable_thinking=False,
        timeout_seconds=12,
        max_output_tokens=345,
    )


def test_endpoint_config_uses_only_existing_endpoint_and_key_names() -> None:
    config = ModelEndpointConfig.from_env(
        {
            "FLYBOARD_QWEN_API_BASE": "https://example.test/compatible-mode/v1/",
            "FLYBOARD_QWEN_API_KEY": "secret",
            "FLYBOARD_QWEN_MODEL": "must-not-override-policy",
        }
    )
    assert config.chat_completions_url == (
        "https://example.test/compatible-mode/v1/chat/completions"
    )
    assert config.api_key == "secret"

    with pytest.raises(ValueError, match="FLYBOARD_QWEN_API_KEY"):
        ModelEndpointConfig.from_env({"FLYBOARD_QWEN_API_BASE": "https://example.test/v1"})


@pytest.mark.parametrize(
    "api_base",
    [
        "https://example.test/v1/chat/completions",
        "https://example.test/v1/chat/completions/",
    ],
)
def test_transport_serializes_policy_messages_and_images(api_base: str) -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.7-plus-2026-05-26",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
            headers={"x-request-id": "req_1"},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    transport = HttpxModelTransport(
        ModelEndpointConfig(api_base=api_base, api_key="secret"),
        client=http_client,
    )
    response = transport.send(
        ModelCallRequest(
            call_purpose=CallPurpose.XLSX,
            messages=(
                ModelMessage(role="system", content=(TextContentPart("system"),)),
                ModelMessage(
                    role="user",
                    content=(
                        TextContentPart("look"),
                        ImageContentPart(data=b"png", media_type="image/png"),
                    ),
                ),
            ),
        ),
        policy(),
        timeout_seconds=12,
    )

    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer secret"
    payload = captured["payload"]
    assert payload == {
        "model": "qwen3.7-plus-2026-05-26",
        "messages": [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,cG5n"},
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "enable_thinking": False,
        "max_tokens": 345,
    }
    assert response.status_code == 200
    assert response.body["choices"][0]["message"]["content"] == "ok"


def test_transport_maps_pre_response_failures_and_invalid_json() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    transport = HttpxModelTransport(
        ModelEndpointConfig(api_base="https://example.test/v1", api_key="secret"),
        client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    request = ModelCallRequest(
        call_purpose=CallPurpose.XLSX,
        messages=(ModelMessage(role="user", content=(TextContentPart("input"),)),),
    )
    with pytest.raises(TransportFailure) as captured:
        transport.send(request, policy(), timeout_seconds=12)
    assert captured.value.reason == TransportFailureReason.TIMEOUT

    invalid_transport = HttpxModelTransport(
        ModelEndpointConfig(api_base="https://example.test/v1", api_key="secret"),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(502, text="not json"))
        ),
    )
    response = invalid_transport.send(request, policy(), timeout_seconds=12)
    assert response.status_code == 502
    assert response.body is None

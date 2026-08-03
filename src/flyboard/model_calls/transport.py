from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .types import (
    ImageContentPart,
    ModelCallPolicy,
    ModelCallRequest,
    RawTransportResponse,
    TextContentPart,
    TransportFailure,
    TransportFailureReason,
)


@dataclass(frozen=True, slots=True)
class ModelEndpointConfig:
    api_base: str
    api_key: str

    def __post_init__(self) -> None:
        base = self.api_base.strip()
        key = self.api_key.strip()
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("model api base must be an absolute http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("model api base must not contain query or fragment")
        if not key:
            raise ValueError("model api key must not be empty")
        object.__setattr__(self, "api_base", base.rstrip("/"))
        object.__setattr__(self, "api_key", key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ModelEndpointConfig:
        values = os.environ if env is None else env
        base = values.get("FLYBOARD_QWEN_API_BASE")
        key = values.get("FLYBOARD_QWEN_API_KEY")
        if base is None or not base.strip():
            raise ValueError("FLYBOARD_QWEN_API_BASE is required")
        if key is None or not key.strip():
            raise ValueError("FLYBOARD_QWEN_API_KEY is required")
        return cls(api_base=base, api_key=key)

    @property
    def chat_completions_url(self) -> str:
        parsed = urlsplit(self.api_base)
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class HttpxModelTransport:
    """OpenAI-compatible transport; it owns only HTTP serialization and I/O facts."""

    def __init__(
        self,
        config: ModelEndpointConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(
        self,
        request: ModelCallRequest,
        policy: ModelCallPolicy,
        *,
        timeout_seconds: float,
    ) -> RawTransportResponse:
        payload = {
            "model": policy.model_id,
            "messages": [_serialize_message(message) for message in request.messages],
            "temperature": policy.temperature,
            "enable_thinking": policy.enable_thinking,
            "max_tokens": policy.max_output_tokens,
        }
        try:
            response = self._client.post(
                self._config.chat_completions_url,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TransportFailure(TransportFailureReason.TIMEOUT) from exc
        except httpx.ConnectError as exc:
            reason = _connect_failure_reason(exc)
            raise TransportFailure(reason) from exc
        except httpx.NetworkError as exc:
            raise TransportFailure(TransportFailureReason.INCOMPLETE_RESPONSE) from exc

        try:
            body: object = response.json()
        except ValueError:
            body = None
        return RawTransportResponse(
            status_code=response.status_code,
            body=body,
            headers=tuple((name, value) for name, value in response.headers.items()),
        )


def _serialize_message(message) -> dict[str, Any]:
    if len(message.content) == 1 and isinstance(message.content[0], TextContentPart):
        content: object = message.content[0].text
    else:
        content = [_serialize_part(part) for part in message.content]
    return {"role": message.role, "content": content}


def _serialize_part(part) -> dict[str, object]:
    if isinstance(part, TextContentPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImageContentPart):
        encoded = base64.b64encode(part.data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{part.media_type};base64,{encoded}"},
        }
    raise TypeError("unsupported model content part")


def _connect_failure_reason(exc: httpx.ConnectError) -> TransportFailureReason:
    text = str(exc).lower()
    if any(marker in text for marker in ("name or service not known", "getaddrinfo", "dns")):
        return TransportFailureReason.DNS_FAILED
    if any(marker in text for marker in ("reset by peer", "connection reset")):
        return TransportFailureReason.CONNECTION_RESET
    return TransportFailureReason.CONNECTION_FAILED

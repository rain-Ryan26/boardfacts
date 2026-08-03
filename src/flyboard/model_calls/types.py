from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias

from flyboard.models import CallPurpose

POLICY_VERSION = "model-call-policy-v1"
MODEL_ID = "qwen3.7-plus-2026-05-26"
TEMPERATURE = 0.0
ENABLE_THINKING = False


class CapacityDecision(StrEnum):
    ALLOWED = "allowed"
    TOO_LARGE = "too_large"
    UNKNOWN = "unknown"


class FailureCategory(StrEnum):
    INPUT_TOO_LARGE = "input_too_large"
    CALL_FAILED = "call_failed"
    RESPONSE_INVALID = "response_invalid"


class TransportFailureReason(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    DNS_FAILED = "dns_failed"
    CONNECTION_RESET = "connection_reset"
    INCOMPLETE_RESPONSE = "incomplete_response"


@dataclass(frozen=True, slots=True)
class ModelCallPolicy:
    policy_version: Literal["model-call-policy-v1"]
    call_purpose: CallPurpose
    model_id: Literal["qwen3.7-plus-2026-05-26"]
    temperature: float
    enable_thinking: Literal[False]
    timeout_seconds: float
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.policy_version != POLICY_VERSION:
            raise ValueError(f"policy_version must be {POLICY_VERSION}")
        if self.model_id != MODEL_ID:
            raise ValueError(f"model_id must be {MODEL_ID}")
        if self.temperature != TEMPERATURE:
            raise ValueError(f"temperature must be {TEMPERATURE}")
        if self.enable_thinking is not ENABLE_THINKING:
            raise ValueError("enable_thinking must be false")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a finite positive number")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        if isinstance(self.max_output_tokens, bool) or not isinstance(self.max_output_tokens, int):
            raise TypeError("max_output_tokens must be a positive integer")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")


@dataclass(frozen=True, slots=True)
class TextContentPart:
    text: str
    type: Literal["text"] = "text"

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("text content must not be empty")


@dataclass(frozen=True, slots=True)
class ImageContentPart:
    data: bytes
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    type: Literal["image"] = "image"

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("image content must not be empty")


ModelContentPart: TypeAlias = TextContentPart | ImageContentPart


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: Literal["system", "user", "assistant"]
    content: tuple[ModelContentPart, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.content, tuple):
            raise TypeError("message content must be a tuple")
        if not self.content:
            raise ValueError("message content must not be empty")
        if not all(isinstance(part, (TextContentPart, ImageContentPart)) for part in self.content):
            raise TypeError("message content contains an unsupported part")


@dataclass(frozen=True, slots=True)
class ModelCallRequest:
    call_purpose: CallPurpose
    messages: tuple[ModelMessage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if not self.messages:
            raise ValueError("messages must not be empty")
        if not all(isinstance(message, ModelMessage) for message in self.messages):
            raise TypeError("messages contains an unsupported value")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("total_tokens", self.total_tokens),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or null")


@dataclass(frozen=True, slots=True)
class CapacityCheckResult:
    decision: CapacityDecision
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.decision == CapacityDecision.TOO_LARGE and not self.reason_code:
            raise ValueError("too_large capacity result requires reason_code")
        if self.reason_code is not None and not self.reason_code.strip():
            raise ValueError("capacity reason_code must not be blank")


@dataclass(frozen=True, slots=True)
class RawTransportResponse:
    status_code: int
    body: object
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("status_code must be an integer")
        if self.status_code < 100 or self.status_code > 599:
            raise ValueError("status_code must be a valid HTTP status")
        if not isinstance(self.headers, tuple):
            raise TypeError("headers must be a tuple")
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
            for item in self.headers
        ):
            raise TypeError("headers must contain string pairs")


class TransportFailure(RuntimeError):
    """A transport failure that happened before a complete HTTP response existed."""

    def __init__(self, reason: TransportFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ModelTransport(Protocol):
    def send(
        self,
        request: ModelCallRequest,
        policy: ModelCallPolicy,
        *,
        timeout_seconds: float,
    ) -> RawTransportResponse: ...


class CapacityPreflight(Protocol):
    def check(self, request: ModelCallRequest, policy: ModelCallPolicy) -> CapacityCheckResult: ...


class ModelCallControl(Protocol):
    def allow_attempt(self, attempt_number: Literal[1, 2]) -> bool: ...


class ModelCaller(Protocol):
    def policy_for(self, call_purpose: CallPurpose) -> ModelCallPolicy: ...

    def call(
        self,
        request: ModelCallRequest,
        *,
        control: ModelCallControl | None = None,
    ) -> ModelCallOutcome: ...


@dataclass(frozen=True, slots=True)
class ModelCallSucceeded:
    call_purpose: CallPurpose
    policy_version: Literal["model-call-policy-v1"]
    request_model_id: str
    response_model_id: str
    capacity_decision: CapacityDecision
    transport_attempt_count: Literal[1, 2]
    transport_retried: bool
    transport_retry_reason: TransportFailureReason | None
    duration_ms: int
    provider_request_id: str | None
    finish_reason: str
    usage: ModelUsage | None
    response_text: str
    status: Literal["succeeded"] = "succeeded"

    def __post_init__(self) -> None:
        _validate_outcome_common(
            self.policy_version,
            self.transport_attempt_count,
            self.transport_retried,
            self.transport_retry_reason,
            self.duration_ms,
        )
        if not self.request_model_id or not self.response_model_id:
            raise ValueError("successful outcome requires request and response model ids")
        if not self.finish_reason:
            raise ValueError("successful outcome requires finish_reason")


@dataclass(frozen=True, slots=True)
class ModelCallFailed:
    call_purpose: CallPurpose
    policy_version: Literal["model-call-policy-v1"]
    request_model_id: str
    response_model_id: str | None
    capacity_decision: CapacityDecision
    transport_attempt_count: Literal[0, 1, 2]
    transport_retried: bool
    transport_retry_reason: TransportFailureReason | None
    duration_ms: int
    provider_request_id: str | None
    finish_reason: str | None
    usage: ModelUsage | None
    failure_category: FailureCategory
    provider_code: str | None
    failure_summary: str
    status: Literal["failed"] = "failed"

    def __post_init__(self) -> None:
        _validate_outcome_common(
            self.policy_version,
            self.transport_attempt_count,
            self.transport_retried,
            self.transport_retry_reason,
            self.duration_ms,
        )
        if not self.request_model_id:
            raise ValueError("failed outcome requires request_model_id")
        if not self.failure_summary:
            raise ValueError("failed outcome requires failure_summary")
        if self.transport_attempt_count == 0:
            if self.failure_category != FailureCategory.INPUT_TOO_LARGE or self.duration_ms != 0:
                raise ValueError("zero-attempt failure must be an input_too_large preflight result")


ModelCallOutcome: TypeAlias = ModelCallSucceeded | ModelCallFailed


def _validate_outcome_common(
    policy_version: str,
    transport_attempt_count: int,
    transport_retried: bool,
    transport_retry_reason: TransportFailureReason | None,
    duration_ms: int,
) -> None:
    if policy_version != POLICY_VERSION:
        raise ValueError(f"policy_version must be {POLICY_VERSION}")
    if transport_attempt_count not in {0, 1, 2}:
        raise ValueError("transport_attempt_count must be 0, 1, or 2")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("duration_ms must be a non-negative integer")
    if transport_retried != (transport_attempt_count == 2):
        raise ValueError("transport_retried must match a second transport attempt")
    if transport_retried != (transport_retry_reason is not None):
        raise ValueError("transport_retry_reason must exist exactly when transport was retried")

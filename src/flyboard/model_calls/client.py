from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Callable, Literal

from .policies import ModelCallPolicyRegistry
from .types import (
    CapacityCheckResult,
    CapacityDecision,
    CapacityPreflight,
    FailureCategory,
    ModelCallControl,
    ModelCallFailed,
    ModelCallOutcome,
    ModelCallRequest,
    ModelCallSucceeded,
    ModelTransport,
    ModelUsage,
    RawTransportResponse,
    TransportFailure,
    TransportFailureReason,
)


class ModelCallUnavailable(RuntimeError):
    """Raised by the fail-closed placeholder when no production client was injected."""


class ModelCallNotStarted(RuntimeError):
    """Control flow raised when stop/business admission blocks the first attempt."""


class UnavailableModelCaller:
    def policy_for(self, call_purpose):
        raise ModelCallUnavailable("production model client is not configured")

    def call(self, request, *, control=None):
        raise ModelCallUnavailable("production model client is not configured")


class UnknownCapacityPreflight:
    def check(self, request, policy) -> CapacityCheckResult:
        return CapacityCheckResult(decision=CapacityDecision.UNKNOWN)


class AlwaysAllowModelCallControl:
    def allow_attempt(self, attempt_number: Literal[1, 2]) -> bool:
        return True


class ModelCallClient:
    def __init__(
        self,
        *,
        policies: ModelCallPolicyRegistry,
        transport: ModelTransport,
        capacity_preflight: CapacityPreflight | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._policies = policies
        self._transport = transport
        self._capacity_preflight = capacity_preflight or UnknownCapacityPreflight()
        self._clock_ns = clock_ns

    def policy_for(self, call_purpose):
        return self._policies.policy_for(call_purpose)

    def call(
        self,
        request: ModelCallRequest,
        *,
        control: ModelCallControl | None = None,
    ) -> ModelCallOutcome:
        policy = self._policies.policy_for(request.call_purpose)
        capacity = self._capacity_preflight.check(request, policy)
        if not isinstance(capacity, CapacityCheckResult):
            raise TypeError("capacity preflight must return CapacityCheckResult")
        if capacity.decision == CapacityDecision.TOO_LARGE:
            return ModelCallFailed(
                call_purpose=request.call_purpose,
                policy_version=policy.policy_version,
                request_model_id=policy.model_id,
                response_model_id=None,
                capacity_decision=capacity.decision,
                transport_attempt_count=0,
                transport_retried=False,
                transport_retry_reason=None,
                duration_ms=0,
                provider_request_id=None,
                finish_reason=None,
                usage=None,
                failure_category=FailureCategory.INPUT_TOO_LARGE,
                provider_code=capacity.reason_code,
                failure_summary="request rejected by deterministic capacity preflight",
            )

        active_control = control or AlwaysAllowModelCallControl()
        if not active_control.allow_attempt(1):
            raise ModelCallNotStarted("model call blocked before the first transport attempt")

        started_ns = self._clock_ns()
        attempt_count: Literal[1, 2] = 1
        retry_reason: TransportFailureReason | None = None
        try:
            response = self._transport.send(
                request,
                policy,
                timeout_seconds=policy.timeout_seconds,
            )
        except TransportFailure as first_failure:
            if not active_control.allow_attempt(2):
                return self._transport_failure_outcome(
                    request=request,
                    capacity=capacity,
                    attempt_count=attempt_count,
                    retry_reason=None,
                    last_reason=first_failure.reason,
                    started_ns=started_ns,
                    retry_blocked=True,
                )
            attempt_count = 2
            retry_reason = first_failure.reason
            try:
                response = self._transport.send(
                    request,
                    policy,
                    timeout_seconds=policy.timeout_seconds,
                )
            except TransportFailure as second_failure:
                return self._transport_failure_outcome(
                    request=request,
                    capacity=capacity,
                    attempt_count=attempt_count,
                    retry_reason=retry_reason,
                    last_reason=second_failure.reason,
                    started_ns=started_ns,
                    retry_blocked=False,
                )

        duration_ms = self._duration_ms(started_ns)
        return self._parse_response(
            request=request,
            response=response,
            capacity=capacity,
            attempt_count=attempt_count,
            retry_reason=retry_reason,
            duration_ms=duration_ms,
        )

    def _transport_failure_outcome(
        self,
        *,
        request: ModelCallRequest,
        capacity: CapacityCheckResult,
        attempt_count: Literal[1, 2],
        retry_reason: TransportFailureReason | None,
        last_reason: TransportFailureReason,
        started_ns: int,
        retry_blocked: bool,
    ) -> ModelCallFailed:
        policy = self._policies.policy_for(request.call_purpose)
        suffix = "; retry blocked by control" if retry_blocked else ""
        return ModelCallFailed(
            call_purpose=request.call_purpose,
            policy_version=policy.policy_version,
            request_model_id=policy.model_id,
            response_model_id=None,
            capacity_decision=capacity.decision,
            transport_attempt_count=attempt_count,
            transport_retried=attempt_count == 2,
            transport_retry_reason=retry_reason,
            duration_ms=self._duration_ms(started_ns),
            provider_request_id=None,
            finish_reason=None,
            usage=None,
            failure_category=FailureCategory.CALL_FAILED,
            provider_code=None,
            failure_summary=f"transport {last_reason.value}{suffix}",
        )

    def _parse_response(
        self,
        *,
        request: ModelCallRequest,
        response: RawTransportResponse,
        capacity: CapacityCheckResult,
        attempt_count: Literal[1, 2],
        retry_reason: TransportFailureReason | None,
        duration_ms: int,
    ) -> ModelCallOutcome:
        if not isinstance(response, RawTransportResponse):
            raise TypeError("transport must return RawTransportResponse")
        policy = self._policies.policy_for(request.call_purpose)
        body = response.body if isinstance(response.body, Mapping) else None
        response_model_id = _optional_short_string(body.get("model")) if body else None
        request_id = _provider_request_id(response, body)
        usage, usage_valid = _parse_usage(body.get("usage")) if body else (None, True)

        if not 200 <= response.status_code < 300:
            provider_code, provider_message = _provider_error_facts(body)
            is_capacity = _is_capacity_rejection(
                status_code=response.status_code,
                provider_code=provider_code,
                provider_message=provider_message,
            )
            return ModelCallFailed(
                call_purpose=request.call_purpose,
                policy_version=policy.policy_version,
                request_model_id=policy.model_id,
                response_model_id=response_model_id,
                capacity_decision=capacity.decision,
                transport_attempt_count=attempt_count,
                transport_retried=attempt_count == 2,
                transport_retry_reason=retry_reason,
                duration_ms=duration_ms,
                provider_request_id=request_id,
                finish_reason=None,
                usage=usage if usage_valid else None,
                failure_category=(
                    FailureCategory.INPUT_TOO_LARGE if is_capacity else FailureCategory.CALL_FAILED
                ),
                provider_code=provider_code,
                failure_summary=(
                    "provider rejected request for capacity"
                    if is_capacity
                    else f"provider rejected request with HTTP {response.status_code}"
                ),
            )

        if body is None:
            return self._invalid_response(
                request=request,
                capacity=capacity,
                attempt_count=attempt_count,
                retry_reason=retry_reason,
                duration_ms=duration_ms,
                response_model_id=None,
                request_id=request_id,
                finish_reason=None,
                usage=None,
                summary="provider envelope is not an object",
            )
        if not usage_valid:
            return self._invalid_response(
                request=request,
                capacity=capacity,
                attempt_count=attempt_count,
                retry_reason=retry_reason,
                duration_ms=duration_ms,
                response_model_id=response_model_id,
                request_id=request_id,
                finish_reason=None,
                usage=None,
                summary="provider usage facts are invalid",
            )

        choices = body.get("choices")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], Mapping)
        ):
            return self._invalid_response(
                request=request,
                capacity=capacity,
                attempt_count=attempt_count,
                retry_reason=retry_reason,
                duration_ms=duration_ms,
                response_model_id=response_model_id,
                request_id=request_id,
                finish_reason=None,
                usage=usage,
                summary="provider envelope must contain exactly one choice",
            )

        choice = choices[0]
        finish_reason = _optional_short_string(choice.get("finish_reason"))
        message = choice.get("message")
        response_text = message.get("content") if isinstance(message, Mapping) else None
        if response_model_id is None or finish_reason is None or not isinstance(response_text, str):
            return self._invalid_response(
                request=request,
                capacity=capacity,
                attempt_count=attempt_count,
                retry_reason=retry_reason,
                duration_ms=duration_ms,
                response_model_id=response_model_id,
                request_id=request_id,
                finish_reason=finish_reason,
                usage=usage,
                summary="provider envelope is missing required response facts",
            )
        if finish_reason in {"length", "max_tokens"}:
            return self._invalid_response(
                request=request,
                capacity=capacity,
                attempt_count=attempt_count,
                retry_reason=retry_reason,
                duration_ms=duration_ms,
                response_model_id=response_model_id,
                request_id=request_id,
                finish_reason=finish_reason,
                usage=usage,
                summary="provider output was truncated",
            )

        return ModelCallSucceeded(
            call_purpose=request.call_purpose,
            policy_version=policy.policy_version,
            request_model_id=policy.model_id,
            response_model_id=response_model_id,
            capacity_decision=capacity.decision,
            transport_attempt_count=attempt_count,
            transport_retried=attempt_count == 2,
            transport_retry_reason=retry_reason,
            duration_ms=duration_ms,
            provider_request_id=request_id,
            finish_reason=finish_reason,
            usage=usage,
            response_text=response_text,
        )

    def _invalid_response(
        self,
        *,
        request: ModelCallRequest,
        capacity: CapacityCheckResult,
        attempt_count: Literal[1, 2],
        retry_reason: TransportFailureReason | None,
        duration_ms: int,
        response_model_id: str | None,
        request_id: str | None,
        finish_reason: str | None,
        usage: ModelUsage | None,
        summary: str,
    ) -> ModelCallFailed:
        policy = self._policies.policy_for(request.call_purpose)
        return ModelCallFailed(
            call_purpose=request.call_purpose,
            policy_version=policy.policy_version,
            request_model_id=policy.model_id,
            response_model_id=response_model_id,
            capacity_decision=capacity.decision,
            transport_attempt_count=attempt_count,
            transport_retried=attempt_count == 2,
            transport_retry_reason=retry_reason,
            duration_ms=duration_ms,
            provider_request_id=request_id,
            finish_reason=finish_reason,
            usage=usage,
            failure_category=FailureCategory.RESPONSE_INVALID,
            provider_code=None,
            failure_summary=summary,
        )

    def _duration_ms(self, started_ns: int) -> int:
        elapsed_ns = self._clock_ns() - started_ns
        return max(0, elapsed_ns // 1_000_000)


def _parse_usage(raw_usage: object) -> tuple[ModelUsage | None, bool]:
    if raw_usage is None:
        return None, True
    if not isinstance(raw_usage, Mapping):
        return None, False
    values = (
        raw_usage.get("prompt_tokens"),
        raw_usage.get("completion_tokens"),
        raw_usage.get("total_tokens"),
    )
    if any(
        value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
        for value in values
    ):
        return None, False
    return ModelUsage(
        input_tokens=values[0],
        output_tokens=values[1],
        total_tokens=values[2],
    ), True


def _provider_request_id(
    response: RawTransportResponse, body: Mapping[object, object] | None
) -> str | None:
    for name, value in response.headers:
        if name.lower() in {"x-request-id", "x-dashscope-request-id"}:
            return _optional_short_string(value)
    return _optional_short_string(body.get("request_id")) if body else None


def _provider_error_facts(body: Mapping[object, object] | None) -> tuple[str | None, str | None]:
    if body is None:
        return None, None
    error = body.get("error")
    if not isinstance(error, Mapping):
        return None, None
    code = _optional_short_string(error.get("code"))
    message = error.get("message")
    return code, message if isinstance(message, str) else None


def _is_capacity_rejection(
    *, status_code: int, provider_code: str | None, provider_message: str | None
) -> bool:
    if status_code == 413:
        return True
    normalized_code = (provider_code or "").lower()
    if normalized_code in {
        "context_length_exceeded",
        "input_too_large",
        "max_tokens_exceeded",
        "request_too_large",
    }:
        return True
    normalized_message = (provider_message or "").lower()
    return any(
        marker in normalized_message
        for marker in (
            "maximum context length",
            "context length exceeded",
            "input is too large",
            "image is too large",
            "too many pixels",
        )
    )


def _optional_short_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    if not cleaned:
        return None
    return cleaned[:200]

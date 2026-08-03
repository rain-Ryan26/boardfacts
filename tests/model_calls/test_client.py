from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from flyboard.model_calls import (
    ENABLE_THINKING,
    MODEL_ID,
    POLICY_VERSION,
    TEMPERATURE,
    CallPurpose,
    CapacityCheckResult,
    CapacityDecision,
    FailureCategory,
    ImageContentPart,
    ModelCallClient,
    ModelCallFailed,
    ModelCallNotStarted,
    ModelCallPolicy,
    ModelCallPolicyRegistry,
    ModelCallRequest,
    ModelCallSucceeded,
    ModelMessage,
    RawTransportResponse,
    TextContentPart,
    TransportFailure,
    TransportFailureReason,
    load_default_policy_registry,
)


def policy_registry() -> ModelCallPolicyRegistry:
    return ModelCallPolicyRegistry(
        tuple(
            ModelCallPolicy(
                policy_version=POLICY_VERSION,
                call_purpose=purpose,
                model_id=MODEL_ID,
                temperature=TEMPERATURE,
                enable_thinking=ENABLE_THINKING,
                timeout_seconds=12.5,
                max_output_tokens=2048,
            )
            for purpose in CallPurpose
        )
    )


def request() -> ModelCallRequest:
    return ModelCallRequest(
        call_purpose=CallPurpose.XLSX,
        messages=(
            ModelMessage(
                role="system",
                content=(TextContentPart(text="Follow the strict XLSX protocol."),),
            ),
            ModelMessage(
                role="user",
                content=(
                    TextContentPart(text="Workbook snapshot"),
                    ImageContentPart(data=b"image-bytes", media_type="image/png"),
                ),
            ),
        ),
    )


def success_response(*, finish_reason="stop", usage=None) -> RawTransportResponse:
    return RawTransportResponse(
        status_code=200,
        headers=(("X-Request-Id", "req_provider_1"),),
        body={
            "model": "qwen3.7-plus-2026-05-26",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": '{"kind":"SUBMIT_XLSX"}'},
                }
            ],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
    )


class ScriptedTransport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def send(self, model_request, policy, *, timeout_seconds):
        self.calls.append((model_request, policy, timeout_seconds))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FixedPreflight:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def check(self, model_request, policy):
        self.calls.append((model_request, policy))
        return self.result


class SequencedControl:
    def __init__(self, allowed):
        self.allowed = iter(allowed)
        self.attempts = []

    def allow_attempt(self, attempt_number):
        self.attempts.append(attempt_number)
        return next(self.allowed)


class SequencedClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def test_default_registry_uses_complete_code_owned_v1_limits() -> None:
    registry = load_default_policy_registry()

    assert {
        policy.call_purpose: (policy.timeout_seconds, policy.max_output_tokens)
        for policy in registry.policies
    } == {
        CallPurpose.PDF_IMAGE: (120.0, 8192),
        CallPurpose.XLSX: (120.0, 16384),
        CallPurpose.GERBER_DIAGNOSTIC: (120.0, 2048),
        CallPurpose.INTEGRATION: (120.0, 8192),
        CallPurpose.RULE_CHECK: (120.0, 2048),
    }


def test_policy_and_request_are_frozen_and_closed() -> None:
    policy = policy_registry().policy_for(CallPurpose.XLSX)
    model_request = request()

    with pytest.raises(FrozenInstanceError):
        policy.timeout_seconds = 1.0
    with pytest.raises(FrozenInstanceError):
        model_request.messages = ()
    with pytest.raises(ValueError, match="finite positive"):
        ModelCallPolicy(
            policy_version=POLICY_VERSION,
            call_purpose=CallPurpose.XLSX,
            model_id=MODEL_ID,
            temperature=TEMPERATURE,
            enable_thinking=ENABLE_THINKING,
            timeout_seconds=0,
            max_output_tokens=1,
        )
    with pytest.raises(TypeError, match="tuple"):
        ModelMessage(role="user", content=[TextContentPart(text="not canonical")])


def test_capacity_rejection_has_zero_attempts_and_does_not_call_transport() -> None:
    model_request = request()
    transport = ScriptedTransport([success_response()])
    preflight = FixedPreflight(
        CapacityCheckResult(CapacityDecision.TOO_LARGE, reason_code="xlsx_input_limit")
    )
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        capacity_preflight=preflight,
    )

    outcome = client.call(model_request)

    assert isinstance(outcome, ModelCallFailed)
    assert outcome.failure_category == FailureCategory.INPUT_TOO_LARGE
    assert outcome.transport_attempt_count == 0
    assert outcome.duration_ms == 0
    assert outcome.provider_code == "xlsx_input_limit"
    assert transport.calls == []
    assert preflight.calls[0][0] is model_request


def test_unknown_capacity_is_preserved_and_request_reaches_transport_unchanged() -> None:
    model_request = request()
    transport = ScriptedTransport([success_response()])
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        capacity_preflight=FixedPreflight(CapacityCheckResult(CapacityDecision.UNKNOWN)),
        clock_ns=SequencedClock([1_000_000_000, 1_007_900_000]),
    )

    outcome = client.call(model_request)

    assert isinstance(outcome, ModelCallSucceeded)
    assert outcome.capacity_decision == CapacityDecision.UNKNOWN
    assert outcome.duration_ms == 7
    assert outcome.provider_request_id == "req_provider_1"
    assert outcome.usage is not None and outcome.usage.total_tokens == 14
    assert transport.calls == [
        (model_request, policy_registry().policy_for(CallPurpose.XLSX), 12.5)
    ]
    assert transport.calls[0][0] is model_request
    assert transport.calls[0][0].messages[1].content[1].data == b"image-bytes"


def test_one_transport_retry_can_succeed_without_adding_a_logical_call() -> None:
    transport = ScriptedTransport(
        [TransportFailure(TransportFailureReason.TIMEOUT), success_response()]
    )
    control = SequencedControl([True, True])
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        clock_ns=SequencedClock([5_000_000_000, 5_025_000_000]),
    )

    outcome = client.call(request(), control=control)

    assert isinstance(outcome, ModelCallSucceeded)
    assert outcome.transport_attempt_count == 2
    assert outcome.transport_retried is True
    assert outcome.transport_retry_reason == TransportFailureReason.TIMEOUT
    assert outcome.duration_ms == 25
    assert control.attempts == [1, 2]
    assert len(transport.calls) == 2


def test_second_transport_failure_ends_the_call() -> None:
    transport = ScriptedTransport(
        [
            TransportFailure(TransportFailureReason.CONNECTION_FAILED),
            TransportFailure(TransportFailureReason.CONNECTION_RESET),
        ]
    )
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        clock_ns=SequencedClock([10_000_000_000, 10_041_000_000]),
    )

    outcome = client.call(request())

    assert isinstance(outcome, ModelCallFailed)
    assert outcome.failure_category == FailureCategory.CALL_FAILED
    assert outcome.transport_attempt_count == 2
    assert outcome.transport_retry_reason == TransportFailureReason.CONNECTION_FAILED
    assert outcome.failure_summary == "transport connection_reset"
    assert len(transport.calls) == 2


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_complete_http_rejection_is_never_retried(status_code) -> None:
    response = RawTransportResponse(
        status_code=status_code,
        body={"error": {"code": "provider_busy", "message": "try again"}},
    )
    transport = ScriptedTransport([response, success_response()])
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        clock_ns=SequencedClock([1_000_000, 3_000_000]),
    )

    outcome = client.call(request())

    assert isinstance(outcome, ModelCallFailed)
    assert outcome.failure_category == FailureCategory.CALL_FAILED
    assert outcome.transport_attempt_count == 1
    assert outcome.provider_code == "provider_busy"
    assert len(transport.calls) == 1


def test_provider_capacity_rejection_is_classified_without_retry_or_error_body_leak() -> None:
    secret_in_error = "maximum context length; Authorization: secret-token"
    transport = ScriptedTransport(
        [
            RawTransportResponse(
                status_code=400,
                body={
                    "request_id": "provider-capacity-1",
                    "error": {
                        "code": "context_length_exceeded",
                        "message": secret_in_error,
                    },
                },
            )
        ]
    )
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        clock_ns=SequencedClock([1_000_000, 2_000_000]),
    )

    outcome = client.call(request())

    assert isinstance(outcome, ModelCallFailed)
    assert outcome.failure_category == FailureCategory.INPUT_TOO_LARGE
    assert outcome.transport_attempt_count == 1
    assert outcome.provider_request_id == "provider-capacity-1"
    assert secret_in_error not in repr(outcome)
    assert "secret-token" not in outcome.failure_summary


@pytest.mark.parametrize(
    ("response", "summary"),
    [
        (success_response(finish_reason="length"), "truncated"),
        (RawTransportResponse(status_code=200, body={"model": MODEL_ID}), "one choice"),
        (RawTransportResponse(status_code=200, body=["not", "an", "object"]), "not an object"),
    ],
)
def test_invalid_or_truncated_response_is_not_retried(response, summary) -> None:
    transport = ScriptedTransport([response, success_response()])
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        clock_ns=SequencedClock([1_000_000, 2_000_000]),
    )

    outcome = client.call(request())

    assert isinstance(outcome, ModelCallFailed)
    assert outcome.failure_category == FailureCategory.RESPONSE_INVALID
    assert summary in outcome.failure_summary
    assert outcome.transport_attempt_count == 1
    assert len(transport.calls) == 1


def test_control_can_block_a_new_logical_call_before_transport() -> None:
    transport = ScriptedTransport([success_response()])
    control = SequencedControl([False])
    client = ModelCallClient(policies=policy_registry(), transport=transport)

    with pytest.raises(ModelCallNotStarted):
        client.call(request(), control=control)

    assert control.attempts == [1]
    assert transport.calls == []


def test_control_can_block_the_second_attempt_after_a_transport_failure() -> None:
    transport = ScriptedTransport([TransportFailure(TransportFailureReason.DNS_FAILED)])
    control = SequencedControl([True, False])
    client = ModelCallClient(
        policies=policy_registry(),
        transport=transport,
        clock_ns=SequencedClock([1_000_000, 6_000_000]),
    )

    outcome = client.call(request(), control=control)

    assert isinstance(outcome, ModelCallFailed)
    assert outcome.failure_category == FailureCategory.CALL_FAILED
    assert outcome.transport_attempt_count == 1
    assert outcome.transport_retried is False
    assert outcome.transport_retry_reason is None
    assert "retry blocked by control" in outcome.failure_summary
    assert len(transport.calls) == 1

from __future__ import annotations

from dataclasses import dataclass

from .types import (
    ENABLE_THINKING,
    MODEL_ID,
    POLICY_VERSION,
    TEMPERATURE,
    CallPurpose,
    ModelCallPolicy,
)


@dataclass(frozen=True, slots=True)
class ModelCallPolicyRegistry:
    _policies: tuple[ModelCallPolicy, ...]

    def __post_init__(self) -> None:
        if not isinstance(self._policies, tuple):
            raise TypeError("policies must be a tuple")
        purposes = [policy.call_purpose for policy in self._policies]
        if len(purposes) != len(set(purposes)):
            raise ValueError("model call policy purpose must be unique")
        missing = set(CallPurpose).difference(purposes)
        extra = set(purposes).difference(CallPurpose)
        if missing or extra:
            missing_text = ", ".join(sorted(item.value for item in missing)) or "none"
            extra_text = ", ".join(sorted(item.value for item in extra)) or "none"
            raise ValueError(
                f"policy registry must cover every call purpose; missing={missing_text}; extra={extra_text}"
            )

    def policy_for(self, call_purpose: CallPurpose) -> ModelCallPolicy:
        for policy in self._policies:
            if policy.call_purpose == call_purpose:
                return policy
        raise KeyError(call_purpose)

    @property
    def policies(self) -> tuple[ModelCallPolicy, ...]:
        return self._policies


# Current v1 limits were exercised through the non-confidential synthetic S7 product chain.
# They are code-owned product policy, not environment or provider-maximum configuration.
_VALIDATED_LIMITS: tuple[tuple[CallPurpose, float, int], ...] = (
    (CallPurpose.PDF_IMAGE, 120.0, 8192),
    (CallPurpose.XLSX, 120.0, 16384),
    (CallPurpose.GERBER_DIAGNOSTIC, 120.0, 2048),
    (CallPurpose.INTEGRATION, 120.0, 8192),
    (CallPurpose.RULE_CHECK, 120.0, 2048),
)


def load_default_policy_registry() -> ModelCallPolicyRegistry:
    return ModelCallPolicyRegistry(
        tuple(
            ModelCallPolicy(
                policy_version=POLICY_VERSION,
                call_purpose=purpose,
                model_id=MODEL_ID,
                temperature=TEMPERATURE,
                enable_thinking=ENABLE_THINKING,
                timeout_seconds=timeout_seconds,
                max_output_tokens=max_output_tokens,
            )
            for purpose, timeout_seconds, max_output_tokens in _VALIDATED_LIMITS
        )
    )

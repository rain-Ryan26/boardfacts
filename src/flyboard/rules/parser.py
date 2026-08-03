from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from flyboard.models import RuleInstruction


@dataclass(frozen=True, slots=True)
class RuleParseResult:
    instruction: RuleInstruction | None
    rejection_code: str | None
    rejection_message: str | None


class RuleResponseParser:
    def parse(self, response_text: str) -> RuleParseResult:
        try:
            decoded = json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            return RuleParseResult(None, "invalid_json", "规则响应不是完整 JSON。")
        try:
            instruction = RuleInstruction.model_validate(decoded)
        except (ValidationError, ValueError, TypeError):
            return RuleParseResult(None, "invalid_instruction", "规则响应未通过输出合同。")
        return RuleParseResult(instruction, None, None)

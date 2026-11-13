# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Exercise every branch of the intermediate boundary validator.

Runs locally with no AWS calls and no credentials, so this is the fastest way
to confirm the boundary behaves as intended before you deploy it.

Usage:
    python intermediate/test_validator.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from validator import (
    CONFIDENCE_THRESHOLD,
    STATUTORY_MAXIMUM,
    lambda_handler,
    model_reply_handler,
)

VALID_OUTPUT = {
    "category": "housing",
    "amount": 1200,
    "confidence": 0.93,
    "source_citation": "HA-3.2",
}


def check(label, event, expect_valid, expect_reason_contains=None, handler=lambda_handler):
    result = handler(event)
    ok = result["valid"] is expect_valid
    if ok and expect_reason_contains:
        ok = expect_reason_contains.lower() in result.get("reason", "").lower()
    if ok and not expect_valid:
        ok = result.get("action") == "ESCALATE_HUMAN"

    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"        {result}")
    return ok


def main():
    outcomes = [
        check(
            "valid output passes through",
            {"agentOutput": VALID_OUTPUT},
            expect_valid=True,
        ),
        check(
            "absent required fields escalate",
            {"agentOutput": {"category": "housing"}},
            expect_valid=False,
            expect_reason_contains="missing or null",
        ),
        # The model returns null when the record does not support a
        # determination. A presence-only check passes and the value reaches a
        # comparison as None.
        check(
            "null amount is treated as missing, not compared",
            {"agentOutput": {**VALID_OUTPUT, "amount": None}},
            expect_valid=False,
            expect_reason_contains="missing or null",
        ),
        check(
            "null confidence is treated as missing",
            {"agentOutput": {**VALID_OUTPUT, "confidence": None}},
            expect_valid=False,
            expect_reason_contains="missing or null",
        ),
        check(
            "all fields null escalates without raising",
            {"agentOutput": {field: None for field in VALID_OUTPUT}},
            expect_valid=False,
            expect_reason_contains="missing or null",
        ),
        check(
            "string amount escalates rather than raising TypeError",
            {"agentOutput": {**VALID_OUTPUT, "amount": "1200"}},
            expect_valid=False,
            expect_reason_contains="not numeric",
        ),
        check(
            "boolean amount is not accepted as the number 1",
            {"agentOutput": {**VALID_OUTPUT, "amount": True}},
            expect_valid=False,
            expect_reason_contains="not numeric",
        ),
        check(
            "negative amount escalates",
            {"agentOutput": {**VALID_OUTPUT, "amount": -50}},
            expect_valid=False,
            expect_reason_contains="negative",
        ),
        check(
            "confidence above 1 escalates",
            {"agentOutput": {**VALID_OUTPUT, "confidence": 1.4}},
            expect_valid=False,
            expect_reason_contains="outside",
        ),
        check(
            "non-object output escalates",
            {"agentOutput": "not a dict"},
            expect_valid=False,
            expect_reason_contains="not a json object",
        ),
        check(
            "absent agentOutput key escalates",
            {},
            expect_valid=False,
            expect_reason_contains="not a json object",
        ),
        check(
            f"amount above the statutory maximum of {STATUTORY_MAXIMUM} escalates",
            {"agentOutput": {**VALID_OUTPUT, "amount": STATUTORY_MAXIMUM + 1}},
            expect_valid=False,
            expect_reason_contains="statutory maximum",
        ),
        check(
            f"confidence below {CONFIDENCE_THRESHOLD} escalates",
            {"agentOutput": {**VALID_OUTPUT, "confidence": CONFIDENCE_THRESHOLD - 0.1}},
            expect_valid=False,
            expect_reason_contains="confidence",
        ),
        check(
            "amount exactly at the statutory maximum is allowed",
            {"agentOutput": {**VALID_OUTPUT, "amount": STATUTORY_MAXIMUM}},
            expect_valid=True,
        ),
        check(
            f"confidence exactly at {CONFIDENCE_THRESHOLD} is allowed",
            {"agentOutput": {**VALID_OUTPUT, "confidence": CONFIDENCE_THRESHOLD}},
            expect_valid=True,
        ),
        check(
            "a citation that names no rule escalates",
            {"agentOutput": {**VALID_OUTPUT, "source_citation": "reviewed-manually"}},
            expect_valid=False,
            expect_reason_contains="not a rule",
        ),
        check(
            "a rule that directs escalation cannot justify an award",
            {"agentOutput": {**VALID_OUTPUT, "source_citation": "G-0.1"}},
            expect_valid=False,
            expect_reason_contains="not a rule",
        ),
        check(
            "the statutory ceiling is a valid citation",
            {"agentOutput": {**VALID_OUTPUT, "source_citation": "G-0.2"}},
            expect_valid=True,
        ),
        # model_reply_handler parses the raw Bedrock reply. Each case below is
        # a shape seen from Claude Sonnet 5 through InvokeModel.
        check(
            "reply led by a thinking block is parsed from the text block",
            {"modelContent": [{"type": "thinking", "thinking": ""},
                              {"type": "text", "text": json.dumps(VALID_OUTPUT)}]},
            expect_valid=True,
            handler=model_reply_handler,
        ),
        check(
            "prose reply escalates rather than failing the execution",
            {"modelContent": [{"type": "text", "text": "The applicant qualifies for 1200."}]},
            expect_valid=False,
            expect_reason_contains="not json",
            handler=model_reply_handler,
        ),
        check(
            "reply with no text block escalates",
            {"modelContent": [{"type": "thinking", "thinking": ""}]},
            expect_valid=False,
            expect_reason_contains="0 text blocks",
            handler=model_reply_handler,
        ),
        check(
            "absent content escalates",
            {},
            expect_valid=False,
            expect_reason_contains="no content blocks",
            handler=model_reply_handler,
        ),
        check(
            "parsed reply still faces every validation rule",
            {"modelContent": [{"type": "text", "text": json.dumps({**VALID_OUTPUT, "amount": 9999999})}]},
            expect_valid=False,
            expect_reason_contains="statutory maximum",
            handler=model_reply_handler,
        ),
    ]

    passed = sum(outcomes)
    print(f"\n{passed}/{len(outcomes)} PASS")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Intermediate boundary: validate structured output between agent steps.

Without a check between steps, errors compound: a step that succeeds 90% of the
time drops to 59% across five sequential steps.

Deploy as a Lambda function with model_reply_handler as its handler and invoke
it from the ValidateOutput task in control-safety/state_machine.asl.json.

Three rules this validator follows that are easy to get wrong:

  1. A field that is present but null is missing. When you correctly instruct a
     model not to invent values, it returns null, and a naive `key not in
     output` check passes. The value then reaches a comparison as None.

  2. Type-check before comparing. A model may return "1200" rather than 1200.
     In Python that raises TypeError. In Cedar it silently denies. Never
     compare a value whose type you have not confirmed.

  3. Never raise. A validator that throws converts a validation decision into
     an error path. The reason is lost, the boundary metric never records the
     violation, and correctness now depends on a Catch clause being present.
     Return a verdict instead, always.

The ceiling and threshold are module constants here for clarity. In production,
read them from AWS Systems Manager Parameter Store or AWS AppConfig so a change
is audited and takes effect without a redeploy.
"""

import json

REQUIRED_FIELDS = ["category", "amount", "confidence", "source_citation"]

# A citation must name a rule that sets an award. G-0.1 and G-0.3 direct
# escalation, so an award citing them contradicts its own source.
AWARD_RULES = {"HA-3.2", "HA-4.1", "FA-1.4", "EN-2.1", "G-0.2"}

# Program-specific statutory maximum. The gateway Cedar policy in
# gateway-authorization/award_threshold.cedar enforces the same value, and
# test_tool_schema.py checks that the two match.
STATUTORY_MAXIMUM = 50000

# Treat this as a policy dial you tune, not a value the model reports about
# itself. See the note on calibration below.
CONFIDENCE_THRESHOLD = 0.8


def _escalate(reason):
    return {"valid": False, "reason": reason, "action": "ESCALATE_HUMAN"}


def _as_number(value):
    """Return value as a number, or None if it is not one.

    bool is rejected explicitly because it is a subclass of int in Python, so
    True would otherwise pass as the number 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def lambda_handler(event, context=None):
    """Validate one agent step's output before it reaches the award step.

    Every path returns a verdict. Nothing raises, so a malformed output becomes
    a reviewable escalation rather than a failed execution.
    """
    output = event.get("agentOutput")
    if not isinstance(output, dict):
        return _escalate("Step output was not a JSON object")

    # A null value is a missing value. The model reporting null is correct
    # behavior when the record does not support a determination.
    missing = [field for field in REQUIRED_FIELDS if output.get(field) is None]
    if missing:
        return _escalate(f"Missing or null fields: {missing}")

    if output["source_citation"] not in AWARD_RULES:
        return _escalate(f"Citation is not a rule that sets an award: {output['source_citation']!r}")

    amount = _as_number(output["amount"])
    if amount is None:
        return _escalate(f"Amount was not numeric: {output['amount']!r}")
    if amount < 0:
        return _escalate(f"Amount was negative: {amount}")
    if amount > STATUTORY_MAXIMUM:
        return _escalate("Amount exceeds statutory maximum")

    confidence = _as_number(output["confidence"])
    if confidence is None:
        return _escalate(f"Confidence was not numeric: {output['confidence']!r}")
    if not 0.0 <= confidence <= 1.0:
        return _escalate(f"Confidence outside 0-1: {confidence}")

    # A model's self-reported confidence is not a measurement. Do not ask a
    # model to grade its own certainty and then trust the answer. Derive this
    # value from token log-probabilities or a separate judge model.
    if confidence < CONFIDENCE_THRESHOLD:
        return _escalate("Confidence below threshold")

    return {"valid": True, "passthrough": output}


def model_reply_handler(event, context=None):
    """Parse a raw Bedrock InvokeModel reply, then validate it.

    Set this as the Lambda handler when the state machine passes the model's
    content blocks unparsed. Parsing belongs here rather than in a
    States.StringToJson call: a reply that is prose, or that leads with a
    thinking block, makes the intrinsic fail with States.Runtime, which no
    Catch handles, so the execution fails instead of escalating.
    """
    content = event.get("modelContent")
    if not isinstance(content, list):
        return _escalate("Model reply had no content blocks")
    texts = [
        block.get("text")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if len(texts) != 1 or not isinstance(texts[0], str):
        return _escalate(f"Model reply had {len(texts)} text blocks, expected 1")
    try:
        output = json.loads(texts[0])
    except ValueError:
        return _escalate("Model reply was not JSON")
    return lambda_handler({"agentOutput": output}, context)

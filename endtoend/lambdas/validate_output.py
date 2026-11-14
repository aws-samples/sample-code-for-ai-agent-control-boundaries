# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Boundary 2: the intermediate boundary, deployed.

Same logic as intermediate/validator.py, with the CloudWatch emission that makes
a violation observable. Kept self-contained so the deployment package needs no
shared layer.

The rules it enforces:
  1. A field present but null is missing.
  2. Type-check before comparing.
  3. Never raise. A raising validator loses the reason, skips the metric, and
     makes correctness depend on a Catch clause existing.
"""

import os

import boto3

REGION = os.environ["AWS_REGION"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "AgentBoundaries")
STATUTORY_MAXIMUM = int(os.environ.get("STATUTORY_MAXIMUM", "50000"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))

REQUIRED_FIELDS = ["category", "amount", "confidence", "source_citation"]

# A citation must name a rule that sets an award. G-0.1 and G-0.3 direct
# escalation, so an award citing them contradicts its own source.
AWARD_RULES = {"HA-3.2", "HA-4.1", "FA-1.4", "EN-2.1", "G-0.2"}

cloudwatch = boto3.client("cloudwatch", region_name=REGION)


def emit(violated):
    """Publish at two granularities. CloudWatch does not aggregate across
    dimensions for custom metrics, so an alarm on BoundaryType alone never
    receives data unless that exact dimension set is also published."""
    value = 1 if violated else 0
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": "BoundaryViolation",
                "Dimensions": [
                    {"Name": "BoundaryType", "Value": "Intermediate"},
                    {"Name": "BoundaryName", "Value": "validate-output"},
                ],
                "Value": value,
                "Unit": "Count",
            },
            {
                "MetricName": "BoundaryViolation",
                "Dimensions": [{"Name": "BoundaryType", "Value": "Intermediate"}],
                "Value": value,
                "Unit": "Count",
            },
        ],
    )


def _escalate(reason):
    emit(violated=True)
    return {"valid": False, "reason": reason, "action": "ESCALATE_HUMAN"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def lambda_handler(event, context=None):
    output = event.get("agentOutput")
    if not isinstance(output, dict):
        return _escalate("Step output was not a JSON object")

    missing = [f for f in REQUIRED_FIELDS if output.get(f) is None]
    if missing:
        return _escalate(f"Missing or null fields: {missing}")

    if output["source_citation"] not in AWARD_RULES:
        return _escalate(f"Citation is not a rule that sets an award: {output['source_citation']!r}")

    amount = _number(output["amount"])
    if amount is None:
        return _escalate(f"Amount was not numeric: {output['amount']!r}")
    if amount < 0:
        return _escalate(f"Amount was negative: {amount}")
    if amount > STATUTORY_MAXIMUM:
        return _escalate("Amount exceeds statutory maximum")

    confidence = _number(output["confidence"])
    if confidence is None:
        return _escalate(f"Confidence was not numeric: {output['confidence']!r}")
    if not 0.0 <= confidence <= 1.0:
        return _escalate(f"Confidence outside 0-1: {confidence}")
    if confidence < CONFIDENCE_THRESHOLD:
        return _escalate("Confidence below threshold")

    emit(violated=False)
    return {"valid": True, "passthrough": output}

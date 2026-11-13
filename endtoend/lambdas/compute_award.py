# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Boundary 4: the control boundary applied to arithmetic.

The measured result in evaluation/ was that a schema boundary does not reduce
substantively wrong awards, because a wrong-but-well-formed number passes every
structural check. Moving the rule application into code removed the error class
entirely.

So this handler, not the model, computes the award. The model supplies facts.
Code supplies the determination and the citation.
"""

import os

import boto3

REGION = os.environ["AWS_REGION"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "AgentBoundaries")
STATUTORY_MAXIMUM = int(os.environ.get("STATUTORY_MAXIMUM", "50000"))

cloudwatch = boto3.client("cloudwatch", region_name=REGION)


def emit(boundary_name, violated):
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
                    {"Name": "BoundaryType", "Value": "Control"},
                    {"Name": "BoundaryName", "Value": boundary_name},
                ],
                "Value": value,
                "Unit": "Count",
            },
            {
                "MetricName": "BoundaryViolation",
                "Dimensions": [{"Name": "BoundaryType", "Value": "Control"}],
                "Value": value,
                "Unit": "Count",
            },
        ],
    )


def _number(value):
    """bool is rejected explicitly because it subclasses int in Python."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def compute(facts, category):
    """Apply the published rules. Returns (amount, citation).

    amount None means the rules require escalation rather than a determination.
    """
    if category is None:
        return None, "G-0.3"

    income = _number(facts.get("monthly_income"))
    household = _number(facts.get("household_size"))
    if income is None or household is None:
        return None, "G-0.1"
    # A documented value outside its possible range is not documented.
    if income < 0 or household < 1:
        return None, "G-0.1"

    one_time = facts.get("award_type") == "one_time"

    if category == "housing":
        if one_time:
            requested = _number(facts.get("requested_amount"))
            if requested is None or requested < 0:
                return None, "G-0.1"
            total = min(requested, 5000)
            citation = "HA-4.1"
        else:
            rent = _number(facts.get("monthly_rent"))
            if rent is None or rent < 0:
                return None, "G-0.1"
            monthly = min(rent - 0.30 * income, 900)
            if monthly <= 0:
                return 0, "HA-3.2"
            # HA-3.2: use 12 only when no period is stated. A stated zero or
            # negative period is not a period the rules can apply.
            months = _number(facts.get("months_requested"))
            if months is None:
                months = 12
            elif months <= 0:
                return None, "G-0.1"
            total = monthly * months
            citation = "HA-3.2"
    elif category == "food_assistance":
        total = min(200 * household, 800) * 12
        citation = "FA-1.4"
    elif category == "energy":
        total = 400
        citation = "EN-2.1"
    else:
        return None, "G-0.3"

    # G-0.2 statutory ceiling. When it binds, G-0.2 produced the number.
    total = int(round(total))
    if total > STATUTORY_MAXIMUM:
        return STATUTORY_MAXIMUM, "G-0.2"
    return total, citation


def lambda_handler(event, context=None):
    facts = event.get("facts") or {}
    category = event.get("category")

    amount, citation = compute(facts, category)

    if amount is None:
        emit("compute-award", violated=True)
        return {
            "caseId": event["caseId"],
            "boundary": "control",
            "escalate": True,
            "reason": f"Rules require escalation ({citation})",
            "source_citation": citation,
            "category": category,
        }

    emit("compute-award", violated=False)
    return {
        "caseId": event["caseId"],
        "escalate": False,
        "agentOutput": {
            "category": category,
            "amount": amount,
            "source_citation": citation,
            # Deterministic given the facts, so this reflects that the rules
            # were applied in code, not a model's self-assessment.
            "confidence": 1.0,
        },
    }

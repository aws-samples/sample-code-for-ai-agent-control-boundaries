# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The deployed alarm watches a metric series the deployed Lambdas publish.

Offline. CloudWatch does not aggregate custom metrics across dimensions, so an
alarm whose dimension set no publisher sends receives no data and stays in OK.
This suite reads the alarm from deploy.py and records what the Lambdas publish
when their own code runs against a fake CloudWatch client.
"""

import ast
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
DEPLOY = HERE / "deploy.py"

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("BEDROCK_MODEL_ID", "test")
os.environ.setdefault("GUARDRAIL_ID", "test")
os.environ.setdefault("GUARDRAIL_VERSION", "DRAFT")
os.environ.setdefault("AWARDS_TABLE", "test")
sys.path.insert(0, str(HERE / "lambdas"))

import classify  # noqa: E402
import compute_award  # noqa: E402
import validate_output  # noqa: E402

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        {detail}")
        failures.append(label)


class FakeCloudWatch:
    def __init__(self):
        self.series = set()

    def put_metric_data(self, Namespace, MetricData):
        for datum in MetricData:
            dims = frozenset((d["Name"], d["Value"]) for d in datum["Dimensions"])
            self.series.add((Namespace, datum["MetricName"], dims))


def deployed_alarm():
    """Read the put_metric_alarm keywords from deploy.py without importing it,
    because importing it checks a live model."""
    for node in ast.walk(ast.parse(DEPLOY.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "put_metric_alarm":
            kw = {k.arg: k.value for k in node.keywords}
            dims = ast.literal_eval(kw["Dimensions"])
            return (
                ast.literal_eval(kw["Namespace"]),
                ast.literal_eval(kw["MetricName"]),
                frozenset((d["Name"], d["Value"]) for d in dims),
            )
    raise RuntimeError("no put_metric_alarm call in deploy.py")


def classify_emit_calls():
    """Every emit(type, name, ...) call in classify.py, read from its source,
    because the handler needs Bedrock to reach them."""
    calls = set()
    for node in ast.walk(ast.parse(Path(classify.__file__).read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "emit":
            args = [a for a in node.args if isinstance(a, ast.Constant)]
            if len(args) >= 2:
                calls.add((args[0].value, args[1].value))
    return calls


def published_series():
    fake = FakeCloudWatch()
    for module in (classify, compute_award, validate_output):
        module.cloudwatch = fake
    award = {"category": "housing", "amount": 7320, "confidence": 1.0, "source_citation": "HA-3.2"}
    validate_output.lambda_handler({"agentOutput": award})
    validate_output.lambda_handler({"agentOutput": {**award, "amount": None}})
    compute_award.lambda_handler({"caseId": "T", "category": "housing",
                                  "facts": {"monthly_income": 1800, "household_size": 3, "monthly_rent": 1150}})
    compute_award.lambda_handler({"caseId": "T", "category": "housing", "facts": {}})
    emit_calls = classify_emit_calls()
    for boundary_type, boundary_name in emit_calls:
        classify.emit(boundary_type, boundary_name, violated=True)
    return fake.series, emit_calls


def main():
    alarm = deployed_alarm()
    series, emit_calls = published_series()
    check(bool(emit_calls), "classify.py emit calls were found", "none parsed")
    check(
        alarm in series,
        f"deployed alarm {dict(alarm[2])} on {alarm[0]}/{alarm[1]} matches a published series",
        "published: " + "; ".join(sorted(f"{n}/{m} {dict(d)}" for n, m, d in series)),
    )
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run the ground-truth cases through the DEPLOYED stack and grade the results.

Every boundary is in the path: the guardrail screens the record, code routes and computes, the
validator gates, and a real human approval step blocks on a task token.

A simulated reviewer consumes task tokens from SQS and responds, so boundary 5
runs end to end. The reviewer approves with the
correct determination when the rules support one, and denies when they do not.

Outcomes:
    auto_correct      recorded automatically, amount matches ground truth
    escalated_correct escalated, and the rules require a human or the
                      award is above the approval threshold
    over_escalated    escalated a case the rules could have decided
    wrong_path        stopped at Classify by a boundary that should have passed it
    denied            reviewer denied
    silent_failure    recorded automatically with the WRONG amount

Usage:
    python endtoend/run_cases.py
    python endtoend/run_cases.py --runs 2
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "evaluation"))
sys.path.insert(0, str(HERE.parent / "intermediate"))

# Ground truth lives in one place so the deployed run and the offline
# evaluation cannot drift apart.
from measure_reliability import CASES  # noqa: E402

STACK = json.loads((HERE / "stack.json").read_text())
REGION = STACK["region"]

sfn = boto3.client("stepfunctions", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
dynamodb = boto3.client("dynamodb", region_name=REGION)

REVIEWER = "reviewer@example.com"


def reviewer_citation(case):
    """The rule a reviewer applies when approving. The validator checks
    approvals too, so a citation that names no award rule is rejected."""
    if "one-time" in case["record"]:
        return "HA-4.1"
    return {"housing": "HA-3.2", "food_assistance": "FA-1.4", "energy": "EN-2.1"}[
        case["expected_category"]
    ]


def approval_threshold():
    """Read the threshold from the state machine so the grader cannot drift from it."""
    definition = json.loads((HERE / "state_machine.asl.json").read_text())
    choice = definition["States"]["CheckApprovalThreshold"]["Choices"][0]
    return choice["NumericGreaterThan"]


APPROVAL_THRESHOLD = approval_threshold()

# Cases the input or output boundary in Classify is meant to stop. C04 carries a
# prompt injection, which the input guardrail blocks.
EXPECTED_CLASSIFY_BLOCKS = {"C04"}

# A case only the deployed stack runs. It is not in the evaluation's CASES, so
# the published evaluation figures are unchanged. Its award is above the
# approval threshold: min(2400 - 0.30 * 1500, 900) * 12 = 10800.
E2E_CASES = CASES + [
    {
        "id": "C09",
        "probe": "valid award above approval threshold",
        "record": "Applicant requests housing aid. Monthly rent 2400. Income verified 1500/mo. Household size 4.",
        "expected_category": "housing",
        "expected_amount": 10800,
    },
]

# States a case must pass through. A correct outcome by another route does not
# prove the boundary it exists to test.
EXPECTED_STATES = {
    "C09": ["CheckApprovalThreshold", "HumanReview", "ValidateApprovedAward", "RecordApprovedAward"],
}


def execution_path(execution_arn):
    """The states entered, in order, and the Classify step's output."""
    entered, classification = [], {}
    kwargs = {"executionArn": execution_arn, "maxResults": 1000}
    while True:
        page = sfn.get_execution_history(**kwargs)
        for event in page["events"]:
            if event["type"].endswith("StateEntered"):
                entered.append(event["stateEnteredEventDetails"]["name"])
            exited = event.get("stateExitedEventDetails")
            if exited and exited["name"] == "Classify":
                classification = json.loads(exited["output"]).get("classification", {})
        if not page.get("nextToken"):
            return entered, classification
        kwargs["nextToken"] = page["nextToken"]


def start_executions(runs):
    """Start one execution per case per run. Case ids are unique per run because
    RecordAward writes conditionally and would reject a replayed case id."""
    stamp = int(time.time())
    launched = {}
    for case in E2E_CASES:
        for run_index in range(runs):
            case_id = f"{case['id']}-r{run_index}-{stamp}"
            execution = sfn.start_execution(
                stateMachineArn=STACK["stateMachineArn"],
                name=case_id,
                input=json.dumps({"caseId": case_id, "record": case["record"]}),
            )
            launched[execution["executionArn"]] = {"case": case, "caseId": case_id}
            print(f"  started {case_id} ({case['probe']})")
    return launched


def serve_review_requests(launched):
    """Act as the reviewer for any pending task token."""
    messages = sqs.receive_message(
        QueueUrl=STACK["queueUrl"],
        MaxNumberOfMessages=10,
        WaitTimeSeconds=5,
        VisibilityTimeout=30,
    ).get("Messages", [])

    served = 0
    for message in messages:
        envelope = json.loads(message["Body"])
        payload = json.loads(envelope["Message"])
        token = payload["taskToken"]
        case_id = payload["caseId"]

        case = next(
            (info["case"] for info in launched.values() if info["caseId"] == case_id),
            None,
        )
        if case is None:
            sqs.delete_message(
                QueueUrl=STACK["queueUrl"], ReceiptHandle=message["ReceiptHandle"]
            )
            continue

        expected = case["expected_amount"]
        if expected is None:
            # The rules do not support a determination. A real
            # reviewer would request documentation. Here that is a denial.
            output = {"approved": False, "approver": REVIEWER}
            decision = "denied"
        else:
            output = {
                "approved": True,
                "approver": REVIEWER,
                "award": {
                    "category": case["expected_category"],
                    "amount": expected,
                    "source_citation": reviewer_citation(case),
                    "confidence": 1.0,
                },
            }
            decision = f"approved {expected}"

        try:
            sfn.send_task_success(taskToken=token, output=json.dumps(output))
            print(f"    reviewer {decision} for {case_id}")
            served += 1
        except sfn.exceptions.TaskTimedOut:
            print(f"    token for {case_id} already timed out")
        except sfn.exceptions.InvalidToken:
            print(f"    token for {case_id} no longer valid")

        sqs.delete_message(
            QueueUrl=STACK["queueUrl"], ReceiptHandle=message["ReceiptHandle"]
        )
    return served


def await_completion(launched, timeout=900):
    """Serve review requests while waiting for every execution to reach a
    terminal state."""
    deadline = time.time() + timeout
    pending = set(launched)
    while pending and time.time() < deadline:
        serve_review_requests(launched)
        for execution_arn in list(pending):
            status = sfn.describe_execution(executionArn=execution_arn)["status"]
            if status != "RUNNING":
                pending.discard(execution_arn)
        if pending:
            time.sleep(3)
    if pending:
        print(f"  WARNING: {len(pending)} execution(s) did not finish")
    return pending


def recorded_award(case_id):
    item = dynamodb.get_item(
        TableName=STACK["table"], Key={"caseId": {"S": case_id}}
    ).get("Item")
    if not item:
        return None
    return {
        "amount": int(item["amount"]["N"]),
        "approvedBy": item["approvedBy"]["S"],
        "citation": item["sourceCitation"]["S"],
    }


def grade(case, case_id, execution_arn):
    detail = sfn.describe_execution(executionArn=execution_arn)
    status = detail["status"]
    award = recorded_award(case_id)
    expected = case["expected_amount"]

    if status == "FAILED":
        return "workflow_failed", f"execution failed: {detail.get('error')}"

    # Check the path as well as the outcome. A case stopped at Classify reaches a
    # human and can grade as a correct escalation even when a boundary blocked
    # it wrongly.
    entered, classification = execution_path(execution_arn)
    missing = [s for s in EXPECTED_STATES.get(case["id"], []) if s not in entered]
    if missing:
        return "wrong_path", f"never entered {missing}; path {' > '.join(entered)}"
    if classification.get("blocked") and case["id"] not in EXPECTED_CLASSIFY_BLOCKS:
        return (
            "wrong_path",
            f"stopped at Classify ({classification.get('boundary')}): "
            f"{classification.get('reason')}",
        )

    if award is None:
        # Nothing was written, so the reviewer denied or the workflow stopped.
        if expected is None:
            return "escalated_correct", "no award, rules require escalation"
        return "over_escalated", f"no award recorded but rules give {expected}"

    if award["approvedBy"] == REVIEWER:
        if expected is None:
            return "escalated_correct", "reviewer decided a case the rules defer"
        if expected > APPROVAL_THRESHOLD:
            # Name the route the history shows, not the one the amount implies.
            route = (
                f"above the {APPROVAL_THRESHOLD} approval threshold"
                if "CheckApprovalThreshold" in entered
                else "escalated before the threshold check"
            )
            return (
                "escalated_correct",
                f"{route}, reviewer approved {award['amount']}",
            )
        return "over_escalated", f"reviewer supplied {award['amount']}"

    # Written automatically.
    if expected is None:
        return (
            "silent_failure",
            f"auto-awarded {award['amount']} when the rules require escalation",
        )
    if award["amount"] != expected:
        return (
            "silent_failure",
            f"auto-awarded {award['amount']} but the rules give {expected}",
        )
    return "auto_correct", f"{award['amount']} under {award['citation']}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    print(f"State machine {STACK['stateMachineArn']}")
    print(f"Model {STACK['modelId']}\n")

    print("starting executions:")
    launched = start_executions(args.runs)

    print("\nwaiting, serving review requests as they arrive:")
    await_completion(launched)

    print("\ngrading against ground truth:")
    buckets = (
        "auto_correct",
        "escalated_correct",
        "over_escalated",
        "wrong_path",
        "denied",
        "silent_failure",
        "workflow_failed",
    )
    counts = dict.fromkeys(buckets, 0)
    rows = []
    for execution_arn, info in launched.items():
        outcome, reason = grade(info["case"], info["caseId"], execution_arn)
        counts[outcome] += 1
        rows.append(
            {
                "case": info["case"]["id"],
                "probe": info["case"]["probe"],
                "outcome": outcome,
                "reason": reason,
            }
        )
        flag = "SILENT FAILURE" if outcome == "silent_failure" else outcome
        print(f"  {info['case']['id']} {info['case']['probe']:28s} {flag:18s} {reason}")

    total = len(launched)
    print(f"\n=== DEPLOYED STACK, ALL BOUNDARIES ACTIVE ({total} executions) ===")
    for bucket in buckets:
        if counts[bucket]:
            print(f"  {bucket:20s} {counts[bucket]}")

    silent = counts["silent_failure"]
    print(
        f"\nWrong awards written to the system of record undetected: "
        f"{silent} of {total} ({silent / total:.0%})"
    )
    safe = counts["auto_correct"] + counts["escalated_correct"] + counts["denied"]
    print(f"Correct or safely deferred: {safe} of {total} ({safe / total:.0%})")
    if counts["wrong_path"]:
        print(
            f"Reached a person by the wrong path: {counts['wrong_path']} "
            "(a boundary blocked a case it should have passed)"
        )
    if counts["over_escalated"]:
        print(
            f"Escalated unnecessarily: {counts['over_escalated']} "
            "(safe, but it consumes reviewer time)"
        )

    (HERE / "run_results.json").write_text(
        json.dumps({"model": STACK["modelId"], "counts": counts, "rows": rows}, indent=2)
    )
    print("\nDetail written to endtoend/run_results.json")


if __name__ == "__main__":
    main()

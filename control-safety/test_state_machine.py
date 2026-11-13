# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Enforce the retry and escalation contract of the Step Functions definitions.

A wrong retry policy still looks right: it names plausible exceptions, the
workflow runs, and nothing reports a problem.

Runs locally with no AWS calls and no credentials.

Usage:
    python control-safety/test_state_machine.py
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DEFINITIONS = [
    REPO / "control-safety" / "state_machine.asl.json",
    REPO / "endtoend" / "state_machine.asl.json",
]

# States.TaskFailed is documented as matching every error except States.Timeout,
# so it behaves as a wildcard. Beside specific names it makes them decorative and
# retries deterministic failures that should escalate immediately.
WILDCARDS = {"States.TaskFailed", "States.ALL"}

# Service exceptions that the optimized integrations report service-prefixed.
# A bare name here matches nothing.
BARE_SERVICE_ERRORS = {
    "ThrottlingException",
    "ServiceQuotaExceededException",
    "ValidationException",
    "AccessDeniedException",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "ResourceNotFoundException",
}

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"        {detail}")
        failures.append(label)
    return ok


def tasks(definition):
    for name, state in definition.get("States", {}).items():
        if state.get("Type") == "Task":
            yield name, state


def optimized_service_prefix(resource):
    """Return the prefix the optimized integration uses, or None.

    arn:aws:states:::bedrock:invokeModel reports errors as Bedrock.*, so the
    prefix is derived from the service segment of the ARN.
    """
    marker = "arn:aws:states:::"
    if not resource.startswith(marker):
        return None
    service = resource[len(marker):].split(":")[0]
    if not service or service == "sns":
        return None
    return service[:1].upper() + service[1:]


def test_no_wildcard_beside_specific_names(path, definition):
    for name, state in tasks(definition):
        for i, retrier in enumerate(state.get("Retry", [])):
            errors = set(retrier.get("ErrorEquals", []))
            mixed = errors & WILDCARDS
            if mixed and errors - WILDCARDS:
                check(
                    False,
                    f"{path.parent.name}/{name} retrier {i}: wildcard not mixed with specific names",
                    f"{sorted(mixed)} sits beside {sorted(errors - WILDCARDS)}; "
                    "the wildcard matches everything, so the specific names have no effect",
                )
                return
    check(
        True,
        f"{path.parent.name}: no retrier mixes a wildcard with specific error names",
    )


def test_optimized_integration_errors_are_prefixed(path, definition):
    offenders = []
    checked = 0
    for name, state in tasks(definition):
        prefix = optimized_service_prefix(state.get("Resource", ""))
        if not prefix:
            continue
        checked += 1
        for retrier in state.get("Retry", []):
            for error in retrier.get("ErrorEquals", []):
                if error in BARE_SERVICE_ERRORS:
                    offenders.append(f"{name}: {error} should be {prefix}.{error}")
    if not checked:
        print(f"[SKIP] {path.parent.name}: no optimized service integrations")
        return
    check(
        not offenders,
        f"{path.parent.name}: optimized integrations use service-prefixed error names",
        "; ".join(offenders),
    )


# The handler behind each plain Lambda task. A plain function task reports an
# uncaught exception by its class name, so a botocore throttle arrives as the
# bare ThrottlingException and a prefixed Bedrock.ThrottlingException matches
# nothing: the mirror image of the optimized-integration rule above. None marks
# a function this repository references but does not ship.
FUNCTION_HANDLERS = {
    "${ClassifyIntentFunctionArn}": None,
    "${ClassifyFunctionArn}": "endtoend/lambdas/classify.py",
    "${ComputeAwardFunctionArn}": "endtoend/lambdas/compute_award.py",
    "${RecordAwardFunctionArn}": "endtoend/lambdas/record_award.py",
}
VALIDATOR_HANDLERS = {
    "control-safety": "intermediate/validator.py",
    "endtoend": "endtoend/lambdas/validate_output.py",
}


def function_tasks(path, definition):
    """Yield (name, state, handler path or None) for plain Lambda tasks."""
    for name, state in tasks(definition):
        resource = state.get("Resource", "")
        if resource.startswith("arn:aws:states:::"):
            continue
        if resource == "${ValidateOutputFunctionArn}":
            handler = VALIDATOR_HANDLERS.get(path.parent.name)
        elif resource in FUNCTION_HANDLERS:
            handler = FUNCTION_HANDLERS[resource]
        else:
            check(False, f"{path.parent.name}/{name}: Lambda task has a known handler",
                  f"{resource} is not in FUNCTION_HANDLERS")
            continue
        yield name, state, handler


def test_function_task_errors_are_unprefixed(path, definition):
    offenders = []
    checked = 0
    for name, state, _ in function_tasks(path, definition):
        checked += 1
        for retrier in state.get("Retry", []):
            for error in retrier.get("ErrorEquals", []):
                prefix = error.split(".", 1)[0] if "." in error else None
                if prefix and prefix not in ("Lambda", "States"):
                    offenders.append(f"{name}: {error} should be {error.split('.', 1)[1]}")
    check(
        checked > 0 and not offenders,
        f"{path.parent.name}: Lambda tasks retry bare service error names",
        "; ".join(offenders) or "no Lambda tasks found",
    )


def test_bedrock_callers_retry_throttling(path, definition):
    """A function that calls Bedrock retries its throttle rather than sending a
    transient error to human review."""
    missing = []
    for name, state, handler in function_tasks(path, definition):
        if handler is None:
            continue
        source = (REPO / handler).read_text()
        if "bedrock-runtime" not in source:
            continue
        names = {e for r in state.get("Retry", []) for e in r.get("ErrorEquals", [])}
        if "ThrottlingException" not in names:
            missing.append(name)
    check(
        not missing,
        f"{path.parent.name}: every Lambda task that calls Bedrock retries ThrottlingException",
        f"no ThrottlingException retrier on {missing}",
    )


def test_lambda_tasks_can_escalate(path, definition):
    """A Lambda task can fail on a service error or throttle even when its code
    never raises, so each one needs a Catch, retried or not."""
    stranded = [name for name, state, _ in function_tasks(path, definition)
                if not state.get("Catch")]
    check(
        not stranded,
        f"{path.parent.name}: every Lambda task has a Catch",
        f"no Catch on {stranded}, so a Lambda service error fails the execution",
    )


def test_award_writes_are_checked(path, definition):
    """A refused conditional write returns recorded: false and does not raise,
    so the state after the write must read it or the execution reports
    Succeeded for a write that never happened."""
    states = definition.get("States", {})
    writes = [(n, s) for n, s in tasks(definition)
              if s.get("Resource") == "${RecordAwardFunctionArn}"]
    if not writes:
        return
    unchecked = []
    for name, state in writes:
        nxt = states.get(state.get("Next"), {})
        field = f"{state.get('ResultPath', '$')}.recorded"
        if state.get("End") or nxt.get("Type") != "Choice" or field not in json.dumps(nxt):
            unchecked.append(name)
    check(
        not unchecked,
        f"{path.parent.name}: every award write is followed by a check of recorded",
        f"nothing reads the write's result after {unchecked}",
    )


def test_retried_tasks_can_escalate(path, definition):
    """A task worth retrying is a task whose final failure must go somewhere."""
    stranded = [
        name
        for name, state in tasks(definition)
        if state.get("Retry") and not state.get("Catch")
    ]
    check(
        not stranded,
        f"{path.parent.name}: every retried task has a Catch",
        f"no Catch on {stranded}, so exhausted retries fail the execution",
    )


def test_task_token_states_have_a_timeout(path, definition):
    """A waitForTaskToken state with no timeout waits for a year by default."""
    missing = [
        name
        for name, state in tasks(definition)
        if "waitForTaskToken" in state.get("Resource", "")
        and not state.get("TimeoutSeconds")
        and not state.get("HeartbeatSeconds")
    ]
    check(
        not missing,
        f"{path.parent.name}: every waitForTaskToken state sets a timeout",
        f"{missing} would block until the execution limit",
    )


def test_every_state_is_reachable(path, definition):
    states = definition.get("States", {})
    reachable = {definition.get("StartAt")}
    for state in states.values():
        for key in ("Next", "Default"):
            if state.get(key):
                reachable.add(state[key])
        for choice in state.get("Choices", []):
            if choice.get("Next"):
                reachable.add(choice["Next"])
        for catch in state.get("Catch", []):
            if catch.get("Next"):
                reachable.add(catch["Next"])
    orphans = sorted(set(states) - reachable)
    check(
        not orphans,
        f"{path.parent.name}: every state is reachable",
        f"unreachable: {orphans}",
    )


def test_catches_into_review_keep_the_input(path, definition):
    """A Catch without ResultPath replaces the state input with the error object,
    so a review step that reads $.caseId then fails with States.Runtime."""
    states = definition.get("States", {})
    review = {
        name for name, state in states.items()
        if "waitForTaskToken" in str(state.get("Resource", ""))
    }
    bad = [
        f"{name} -> {catch['Next']}"
        for name, state in states.items()
        for catch in state.get("Catch", [])
        if catch.get("Next") in review and "ResultPath" not in catch
    ]
    check(
        not bad,
        f"{path.parent.name}: every Catch into human review keeps the input",
        f"Catch without ResultPath: {bad}",
    )


def test_approved_awards_are_validated(path, definition):
    """A human approval reaches the write only through the validator, so an
    approver cannot record an award above the statutory ceiling."""
    states = definition.get("States", {})
    if "RecordApprovedAward" not in states:
        return
    into_write = [
        name for name, state in states.items()
        if state.get("Type") == "Choice"
        and (
            any(c.get("Next") == "RecordApprovedAward" for c in state.get("Choices", []))
            or state.get("Default") == "RecordApprovedAward"
        )
    ]
    validated = states.get("ValidateApprovedAward", {}).get("Next") == "CheckApprovedVerdict"
    reads_validated = "approvedValidation" in json.dumps(
        states["RecordApprovedAward"].get("Parameters", {})
    )
    check(
        into_write == ["CheckApprovedVerdict"] and validated and reads_validated,
        f"{path.parent.name}: human-approved awards pass the validator before the write",
        f"routes into RecordApprovedAward: {into_write}; validator wired: {validated}; "
        f"writes validated award: {reads_validated}",
    )


def test_large_awards_wait_for_a_human(path, definition):
    """In the end-to-end stack, a valid award reaches the automated write only
    through the approval-threshold check, which sends larger awards to review."""
    states = definition.get("States", {})
    if "RecordAward" not in states:
        return
    into_write = [
        name for name, state in states.items()
        if state.get("Type") == "Choice"
        and (
            any(c.get("Next") == "RecordAward" for c in state.get("Choices", []))
            or state.get("Default") == "RecordAward"
        )
    ]
    gate = states.get("CheckApprovalThreshold", {})
    routes_up = any(
        "NumericGreaterThan" in c and c.get("Next") == "HumanReview"
        for c in gate.get("Choices", [])
    )
    check(
        into_write == ["CheckApprovalThreshold"] and routes_up,
        f"{path.parent.name}: awards above the approval threshold go to human review",
        f"states routing to RecordAward: {into_write}; threshold routes to review: {routes_up}",
    )


def main():
    for path in DEFINITIONS:
        print(f"\n{path.relative_to(REPO)}")
        definition = json.loads(path.read_text())
        test_no_wildcard_beside_specific_names(path, definition)
        test_optimized_integration_errors_are_prefixed(path, definition)
        test_function_task_errors_are_unprefixed(path, definition)
        test_bedrock_callers_retry_throttling(path, definition)
        test_retried_tasks_can_escalate(path, definition)
        test_lambda_tasks_can_escalate(path, definition)
        test_task_token_states_have_a_timeout(path, definition)
        test_every_state_is_reachable(path, definition)
        test_large_awards_wait_for_a_human(path, definition)
        test_catches_into_review_keep_the_input(path, definition)
        test_approved_awards_are_validated(path, definition)
        test_award_writes_are_checked(path, definition)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

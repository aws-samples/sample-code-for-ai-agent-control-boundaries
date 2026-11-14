# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confirm the tool schema and the Cedar policy agree, and agree with the validator.

A tool parameter declared `number` reaches Cedar as a string, so
`context.input.amount <= 50000` compares a string against an integer and falls
through to the default deny. The policy reads correctly and denies everything.

The Cedar ceiling and the validator's STATUTORY_MAXIMUM are the same number in
two places, as defense in depth. These checks confirm the two copies match.

Runs locally with no AWS calls and no credentials.

Usage:
    python gateway-authorization/test_tool_schema.py
"""

import contextlib
import io
import json
import re
import sys
from pathlib import Path

from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

SCHEMA = HERE / "tool_schema.json"
POLICY = HERE / "award_threshold.cedar"

sys.path.insert(0, str(REPO / "intermediate"))
sys.path.insert(0, str(HERE))
from validator import STATUTORY_MAXIMUM  # noqa: E402

# Importing creates boto3 clients but makes no AWS calls.
import setup_gateway  # noqa: E402

# Keys the gateway's inline tool schema accepts for a property. Anything else,
# such as enum, is rejected by CreateGatewayTarget.
ALLOWED_PROPERTY_KEYS = {"type", "properties", "required", "items", "description"}

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"        {detail}")
        failures.append(label)
    return ok


def strip_comments(cedar_text):
    return re.sub(r"//[^\n]*", "", cedar_text)


def main():
    schema = json.loads(SCHEMA.read_text())
    policy_raw = POLICY.read_text()
    policy = strip_comments(policy_raw)

    properties = schema["inputSchema"]["properties"]

    # A numeric parameter typed `number` arrives as a string.
    numeric_as_number = [
        name for name, spec in properties.items() if spec.get("type") == "number"
    ]
    check(
        not numeric_as_number,
        "no parameter is declared number",
        f"{numeric_as_number} typed number will reach Cedar as a string and "
        "compare as a string, so a numeric condition denies by default; use integer",
    )

    # Every parameter the policy compares numerically must be an integer in the schema.
    compared = set(re.findall(r"context\.input\.(\w+)\s*(?:<=|>=|<|>)", policy))
    check(
        bool(compared),
        "the policy performs at least one numeric comparison on tool input",
        "nothing matched context.input.<name> with a comparison operator; "
        "if the policy changed shape, this test needs updating rather than deleting",
    )
    mistyped = [
        name
        for name in compared
        if properties.get(name, {}).get("type") != "integer"
    ]
    check(
        not mistyped,
        "every numerically compared parameter is declared integer in the schema",
        f"{mistyped} compared numerically but not typed integer in {SCHEMA.name}",
    )

    # Every parameter the policy references must exist, or the condition is unsatisfiable.
    referenced = set(re.findall(r"context\.input\.(\w+)", policy))
    unknown = sorted(referenced - set(properties))
    check(
        not unknown,
        "every parameter the policy references exists in the tool schema",
        f"{unknown} referenced by the policy but absent from the schema",
    )

    # The ceiling appears in two places. Confirm the copies match.
    ceilings = [int(n) for n in re.findall(r"context\.input\.amount\s*<=\s*(\d+)", policy)]
    check(
        len(ceilings) == 1,
        "the policy states the award ceiling exactly once",
        f"found {ceilings}",
    )
    if ceilings:
        check(
            ceilings[0] == STATUTORY_MAXIMUM,
            "the Cedar ceiling matches validator.STATUTORY_MAXIMUM",
            f"Cedar allows up to {ceilings[0]} but the validator enforces "
            f"{STATUTORY_MAXIMUM}; defense in depth only works while the two agree",
        )

    # A placeholder left in a deployed policy is a deny-everything policy.
    check(
        "GATEWAY_ARN_PLACEHOLDER" in policy,
        "the committed policy still carries the gateway ARN placeholder",
        "a real ARN in the committed file would leak an account identifier; "
        "setup_gateway.py substitutes it at deploy time",
    )

    # Cedar rejects a wildcard resource, so the policy must name one gateway.
    check(
        re.search(r"resource\s*==\s*AgentCore::Gateway::", policy) is not None,
        "the policy constrains resource to a specific gateway",
        "a wildcard or absent resource is rejected by Cedar",
    )

    required = set(schema["inputSchema"].get("required", []))
    check(
        required.issubset(set(properties)),
        "every required parameter is defined in properties",
        f"{sorted(required - set(properties))} required but undefined",
    )

    unsupported = {
        name: sorted(set(spec) - ALLOWED_PROPERTY_KEYS)
        for name, spec in properties.items()
        if set(spec) - ALLOWED_PROPERTY_KEYS
    }
    check(
        not unsupported,
        "every property uses only keys the gateway tool schema accepts",
        f"{unsupported} would be rejected by CreateGatewayTarget",
    )

    # The Cedar action is "<target name>___<tool name>". If either half names
    # nothing the gateway exposes, the policy never evaluates a call.
    actions = re.findall(r'AgentCore::Action::"([^"]+)"', policy)
    expected_action = f"{setup_gateway.TARGET_NAME}___{schema['name']}"
    check(
        actions == [expected_action],
        "the Cedar action names the tool the gateway target exposes",
        f"policy names {actions}, but setup_gateway.py exposes {expected_action}",
    )

    # A tool-call policy only evaluates when a target exposes tools. An
    # inference target exposes none, so the policy would enforce nothing.
    config = setup_gateway.build_target_configuration(
        "arn:aws:lambda:us-west-2:123456789012:function:example"
    )
    lambda_config = config.get("mcp", {}).get("lambda", {})
    inline_tools = lambda_config.get("toolSchema", {}).get("inlinePayload", [])
    check(
        [tool.get("name") for tool in inline_tools] == [schema["name"]],
        "the gateway target is an MCP Lambda target exposing this tool",
        f"target configuration is {sorted(config)} with tools "
        f"{[tool.get('name') for tool in inline_tools]}",
    )
    check(
        all(not key.startswith("_") for tool in inline_tools for key in tool),
        "the deployed tool definition carries no explanatory _comment key",
        "the gateway rejects keys outside its tool schema",
    )

    # A rerun after a partial failure hits leftover named resources. main()
    # must say how to recover rather than end in a traceback, and must not
    # swallow any other error.
    real_setup, real_argv = setup_gateway.setup, sys.argv
    sys.argv = ["setup_gateway.py"]
    printed = io.StringIO()
    try:
        for code in ("EntityAlreadyExists", "ConflictException"):
            def leftover(mode, code=code):
                raise ClientError({"Error": {"Code": code, "Message": "exists"}}, "Create")
            setup_gateway.setup = leftover
            with contextlib.redirect_stdout(printed):
                status = setup_gateway.main()
            check(
                status == 1 and "--delete" in printed.getvalue(),
                f"a rerun that hits {code} exits 1 and says to run --delete",
                f"exit {status}, printed {printed.getvalue()!r}",
            )

        def throttled(mode):
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Create")
        setup_gateway.setup = throttled
        try:
            setup_gateway.main()
            check(False, "any other setup error still raises", "main() returned")
        except ClientError:
            check(True, "any other setup error still raises")
    finally:
        setup_gateway.setup, sys.argv = real_setup, real_argv

    # verify() counts only a policy denial as denied. These bodies were
    # captured from a live ENFORCE gateway. The unknown-tool error is a real
    # non-policy failure, which must not pass a check that expects a denial.
    captured = {
        "allowed": {"jsonrpc": "2.0", "id": 1200, "result": {"isError": False, "content": [
            {"type": "text", "text": "{\"recorded\":true,\"amount\":1200}"}]}},
        "denied": {"jsonrpc": "2.0", "id": 60000, "error": {"code": -32002, "message":
            "Tool Execution Denied: Tool call not allowed due to policy enforcement "
            "[No policy applies to the request (denied by default).]"}},
        "error": {"jsonrpc": "2.0", "id": 1200, "error": {"code": -32602,
            "message": "Unknown tool: AwardToolTarget___no_such_tool"}},
    }
    for expected, body in captured.items():
        got = setup_gateway._decision(body)
        check(got == expected, f"a captured {expected} response is classified as {expected}",
              f"classified as {got}")
    for label, body in [
        ("a tool error result", {"result": {"isError": True, "content": []}}),
        ("a throttle", {"error": {"code": -32603, "message": "ThrottlingException: Rate exceeded"}}),
        ("an empty body", {}),
    ]:
        got = setup_gateway._decision(body)
    # Setup order: the policy names the target's tool, so the target must be
    # READY before the policy is created, and the policy must be confirmed
    # ACTIVE. Read the call order from setup()'s source.
    import ast
    import inspect
    calls = [n.func.id for n in ast.walk(ast.parse(inspect.getsource(setup_gateway.setup)))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    order = [c for c in calls if c in
             ("wait_for_target", "create_cedar_policy", "wait_for_policy", "verify")]
    check(order == ["wait_for_target", "create_cedar_policy", "wait_for_policy", "verify"],
          "setup waits for the target, then creates the policy and waits for ACTIVE, then verifies",
          f"order is {order}")

    class FakeAgentCore:
        def __init__(self, statuses):
            self.statuses = list(statuses)

        def get_policy(self, policyEngineId, policyId):
            status = self.statuses.pop(0)
            return {"status": status, "statusReasons": ["unrecognized action"]}

    real_agentcore, real_sleep = setup_gateway.agentcore, setup_gateway.time.sleep
    setup_gateway.time.sleep = lambda seconds: None
    try:
        setup_gateway.agentcore = FakeAgentCore(["CREATING", "ACTIVE"])
        check(setup_gateway.wait_for_policy("e", "p")["status"] == "ACTIVE",
              "wait_for_policy returns once the policy is ACTIVE")
        setup_gateway.agentcore = FakeAgentCore(["CREATING", "CREATE_FAILED"])
        try:
            setup_gateway.wait_for_policy("e", "p")
            check(False, "a CREATE_FAILED policy stops setup with its reason", "returned")
        except RuntimeError as error:
            check("unrecognized action" in str(error),
                  "a CREATE_FAILED policy stops setup with its reason", str(error))
    finally:
        setup_gateway.agentcore, setup_gateway.time.sleep = real_agentcore, real_sleep

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

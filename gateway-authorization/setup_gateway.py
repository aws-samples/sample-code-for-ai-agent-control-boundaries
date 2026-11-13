# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Control boundary at the gateway: Cedar authorization on every tool call.

Creates an AgentCore Gateway with a Cedar policy engine attached, exposes a
record_award tool through a Lambda target, then proves the policy works by
calling the tool twice: once under the ceiling, which must be allowed, and once
over it, which must be denied.

Cedar policies evaluate tool calls, so the policy needs a tool to act on. A
gateway with only an inference target exposes no tools, and a tool-call policy
attached to it never evaluates anything while every setup step reports success.
The final verification step checks for that.

Creation order:

    1. Create the tool's Lambda function and the gateway execution role.
    2. Create the policy engine, empty, and wait for ACTIVE.
    3. Create the gateway referencing the engine ARN.
    4. Create the Lambda target, whose name and tool name form the Cedar
       action, and wait for READY.
    5. Only now create the Cedar policy, because its resource clause must name
       a real gateway ARN and its action must name a tool a target exposes.
       Wait for ACTIVE: a policy that fails validation leaves the gateway
       denying every call, which looks like enforcement.
    6. Call the tool and confirm the policy allows and denies as written.

Usage:
    python gateway-authorization/setup_gateway.py
    python gateway-authorization/setup_gateway.py --mode LOG_ONLY
    python gateway-authorization/setup_gateway.py --delete
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
HTTP = urllib3.PoolManager()

HERE = Path(__file__).parent
CEDAR_PATH = HERE / "award_threshold.cedar"
SCHEMA_PATH = HERE / "tool_schema.json"
TOOL_SOURCE = HERE / "record_award_tool.py"

# Gateway names do not accept hyphens.
GATEWAY_NAME = "bfasamplegw"
ENGINE_NAME = "bfa_sample_policy_engine"
POLICY_NAME = "bfa_sample_award_threshold"
# The Cedar action is "<TARGET_NAME>___<tool name>", so this must match
# award_threshold.cedar. test_tool_schema.py checks that it does.
TARGET_NAME = "AwardToolTarget"
GATEWAY_ROLE_NAME = "bfa-sample-gateway-role"
GATEWAY_INLINE_POLICY = "policy-engine-and-tool-access"
FUNCTION_NAME = "bfa-sample-record-award"
FUNCTION_ROLE_NAME = "bfa-sample-record-award-role"
LAMBDA_BASIC_EXECUTION = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)

# One call the policy must permit and one it must deny.
ALLOWED_AMOUNT = 1200
DENIED_AMOUNT = 60000
NEGATIVE_AMOUNT = -5
# JSON-RPC error code the gateway returns when a Cedar policy denies a call.
POLICY_DENIED_CODE = -32002

# Gateway callers. The agent role is the only principal the Cedar policy
# permits. The other role can reach the gateway through IAM but must be denied
# by Cedar. Both are assumed with temporary credentials, never a long-term user.
AGENT_ROLE_NAME = "bfa-sample-award-agent-role"
OTHER_ROLE_NAME = "bfa-sample-other-caller-role"
CALLER_INLINE_POLICY = "invoke-gateway"

agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
awslambda = boto3.client("lambda", region_name=REGION)
iam = boto3.client("iam")


def load_tool_schema():
    """Return the tool definition, without the explanatory _comment key."""
    schema = json.loads(SCHEMA_PATH.read_text())
    return {key: value for key, value in schema.items() if not key.startswith("_")}


def build_target_configuration(function_arn):
    """An MCP Lambda target: the gateway exposes each schema entry as a tool."""
    return {
        "mcp": {
            "lambda": {
                "lambdaArn": function_arn,
                "toolSchema": {"inlinePayload": [load_tool_schema()]},
            }
        }
    }


def _create_role(name, service, description):
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    return iam.create_role(
        RoleName=name,
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Description=description,
    )["Role"]["Arn"]


def create_tool_function():
    """Deploy the record_award handler, retrying while its new role propagates."""
    role_arn = _create_role(
        FUNCTION_ROLE_NAME, "lambda.amazonaws.com", "Boundary-first sample tool role"
    )
    iam.attach_role_policy(RoleName=FUNCTION_ROLE_NAME, PolicyArn=LAMBDA_BASIC_EXECUTION)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.write(TOOL_SOURCE, TOOL_SOURCE.name)

    for _ in range(10):
        try:
            function_arn = awslambda.create_function(
                FunctionName=FUNCTION_NAME,
                Runtime="python3.12",
                Role=role_arn,
                Handler="record_award_tool.lambda_handler",
                Code={"ZipFile": buffer.getvalue()},
                Timeout=10,
                Description="record_award tool behind the boundary-first sample gateway",
            )["FunctionArn"]
            break
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidParameterValueException":
                raise
            time.sleep(5)
    else:
        raise RuntimeError("Lambda could not assume its role after waiting")

    awslambda.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)
    print(f"Tool function {FUNCTION_NAME} is active")
    return function_arn


def create_execution_role(account, function_arn):
    """Create the gateway execution role with the permissions it needs, and no more.

    Policy evaluation needs GetPolicyEngine, AuthorizeAction, and
    PartiallyAuthorizeActions. Without them, attaching the engine to a gateway
    fails with an access-denied error. Invoking the tool needs
    lambda:InvokeFunction on that one function.
    """
    role_arn = _create_role(
        GATEWAY_ROLE_NAME,
        "bedrock-agentcore.amazonaws.com",
        "Boundary-first sample gateway execution role",
    )
    permissions = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetPolicyEngine",
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{REGION}:{account}:policy-engine/*",
                    f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": function_arn,
            },
        ],
    }
    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName=GATEWAY_INLINE_POLICY,
        PolicyDocument=json.dumps(permissions),
    )
    print(f"Created role {role_arn}, waiting for IAM propagation")
    time.sleep(15)
    return role_arn


def wait_for_engine(engine_id, timeout=120):
    """A new policy engine passes through CREATING and cannot be attached yet."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        engine = agentcore.get_policy_engine(policyEngineId=engine_id)
        status = engine.get("status")
        if status == "ACTIVE":
            return engine
        if status == "FAILED":
            raise RuntimeError(f"Policy engine failed: {engine.get('statusReasons')}")
        time.sleep(5)
    raise TimeoutError(f"Policy engine {engine_id} did not reach ACTIVE")


def wait_for_gateway(gateway_id, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        gateway = agentcore.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway.get("status")
        if status == "READY":
            return gateway
        if status == "FAILED":
            raise RuntimeError(f"Gateway failed: {gateway.get('statusReasons')}")
        time.sleep(5)
    raise TimeoutError(f"Gateway {gateway_id} did not reach READY")


def wait_for_target(gateway_id, target_id, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = agentcore.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        status = target.get("status")
        if status == "READY":
            return target
        if status in ("FAILED", "SYNCHRONIZE_UNSUCCESSFUL"):
            raise RuntimeError(f"Target failed: {target.get('statusReasons')}")
        time.sleep(5)
    raise TimeoutError(f"Target {target_id} did not reach READY")


def create_gateway(role_arn, engine_arn, mode):
    """Create the gateway, retrying while the role's permissions propagate."""
    last_error = None
    for _ in range(6):
        try:
            return agentcore.create_gateway(
                name=GATEWAY_NAME,
                roleArn=role_arn,
                protocolType="MCP",
                authorizerType="AWS_IAM",
                policyEngineConfiguration={"mode": mode, "arn": engine_arn},
                description="Boundary-first sample gateway",
            )
        except ClientError as error:
            message = error.response["Error"]["Message"]
            last_error = message
            if "GetPolicyEngine" in message:
                time.sleep(10)
                continue
            raise
    raise RuntimeError(f"Could not create gateway: {last_error}")


def create_caller_roles(gateway_arn):
    """Create the two gateway caller roles, trusted only by the identity running
    this script, each allowed bedrock-agentcore:InvokeGateway on this gateway."""
    runner_arn = boto3.client("sts").get_caller_identity()["Arn"]
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": runner_arn},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    invoke = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeGateway",
                "Resource": gateway_arn,
            }
        ],
    }
    arns = {}
    for name in (AGENT_ROLE_NAME, OTHER_ROLE_NAME):
        arns[name] = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Boundary-first sample gateway caller",
        )["Role"]["Arn"]
        iam.put_role_policy(
            RoleName=name, PolicyName=CALLER_INLINE_POLICY, PolicyDocument=json.dumps(invoke)
        )
    print(f"Created caller roles {AGENT_ROLE_NAME} and {OTHER_ROLE_NAME}")
    return arns


def assume(role_arn, attempts=12, delay=5):
    """Temporary credentials for a caller role, retrying while the role propagates."""
    sts = boto3.client("sts")
    for _ in range(attempts):
        try:
            c = sts.assume_role(RoleArn=role_arn, RoleSessionName="bfa-sample")["Credentials"]
            return Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        except ClientError as error:
            if error.response["Error"]["Code"] != "AccessDenied":
                raise
            time.sleep(delay)
    raise RuntimeError(f"could not assume {role_arn}")


def create_cedar_policy(engine_id, gateway_arn, agent_principal):
    """Create the Cedar policy against the real gateway ARN.

    Call this only after the tool target is READY. The policy's action names
    the target's tool, and a policy created before the target can end in
    CREATE_FAILED with "unrecognized action". With the target in place,
    FAIL_ON_ANY_FINDINGS rejects any other problem at creation instead of
    leaving a policy that never applies.
    """
    statement = (
        CEDAR_PATH.read_text()
        .replace("GATEWAY_ARN_PLACEHOLDER", gateway_arn)
        .replace("AGENT_PRINCIPAL_PLACEHOLDER", agent_principal)
    )
    created = agentcore.create_policy(
        name=POLICY_NAME,
        policyEngineId=engine_id,
        definition={"cedar": {"statement": statement}},
        enforcementMode="ACTIVE",
        validationMode="FAIL_ON_ANY_FINDINGS",
        description="Award threshold for the record_award tool",
    )
    return created["policyId"]


def wait_for_policy(engine_id, policy_id, timeout=120):
    """A policy that fails validation leaves the gateway denying every call by
    default, which looks like enforcement. Stop with the reason instead."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        policy = agentcore.get_policy(policyEngineId=engine_id, policyId=policy_id)
        status = policy.get("status")
        if status == "ACTIVE":
            return policy
        if status and status.endswith("_FAILED"):
            raise RuntimeError(f"Cedar policy {status}: {policy.get('statusReasons')}")
        time.sleep(5)
    raise TimeoutError(f"Cedar policy {policy_id} did not reach ACTIVE")


def create_tool_target(gateway_id, function_arn):
    """Register the Lambda target. GATEWAY_IAM_ROLE invokes it as the gateway role."""
    created = agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        targetConfiguration=build_target_configuration(function_arn),
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    )
    return created["targetId"]


def call_tool(gateway_url, amount, credentials):
    """Send one SigV4-signed MCP tools/call as the given caller."""
    tool = f"{TARGET_NAME}___{load_tool_schema()['name']}"
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": amount,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {"case_id": "C-1", "program": "housing", "amount": amount},
            },
        }
    )
    request = AWSRequest(
        method="POST",
        url=gateway_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)
    # urllib3 ships with botocore. Unlike urllib.request.urlopen it accepts only
    # http and https URLs, so a file:// or custom scheme cannot be opened.
    response = HTTP.request(
        "POST",
        gateway_url,
        body=body.encode(),
        headers=dict(request.headers),
        timeout=urllib3.Timeout(total=60),
    )
    text = response.data.decode()
    # A streamable HTTP server may answer with a server-sent event.
    if text.startswith("event:") or text.startswith("data:"):
        text = "".join(
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        )
    return json.loads(text)


def _decision(response):
    """Classify a tools/call response as allowed, denied by policy, or error.

    Only a policy denial counts as denied. Any other failure (a throttle, an
    unknown tool, a target that is not ready) is an error, so a broken endpoint
    cannot pass the checks that expect a denial. The denial shape was captured
    from a live ENFORCE gateway: JSON-RPC error -32002, "Tool Execution Denied:
    Tool call not allowed due to policy enforcement".
    """
    result = response.get("result")
    if result is not None and not result.get("isError", False):
        return "allowed"
    error = response.get("error") or {}
    if error.get("code") == POLICY_DENIED_CODE and "policy enforcement" in error.get("message", ""):
        return "denied"
    return "error"


def verify(gateway_url, mode, callers):
    """Prove the boundary fires: the agent role is allowed inside the range, and
    denied outside it; any other role is denied even inside the range."""
    expected_denied = mode == "ENFORCE"
    agent, other = callers[AGENT_ROLE_NAME], callers[OTHER_ROLE_NAME]
    checks = [
        ("agent role", agent, ALLOWED_AMOUNT, True),
        ("agent role", agent, DENIED_AMOUNT, not expected_denied),
        ("agent role", agent, NEGATIVE_AMOUNT, not expected_denied),
        ("other role", other, ALLOWED_AMOUNT, not expected_denied),
    ]
    passed = True
    for who, credentials, amount, should_allow in checks:
        response = call_tool(gateway_url, amount, credentials)
        outcome = _decision(response)
        expected = "allowed" if should_allow else "denied"
        ok = outcome == expected
        passed = passed and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {who}, amount {amount}: {outcome}, expected {expected}")
        if not ok:
            print(f"         response: {json.dumps(response)[:400]}")
    if mode == "LOG_ONLY":
        print("  LOG_ONLY records policy decisions without blocking calls.")
    return passed


def setup(mode):
    account = boto3.client("sts").get_caller_identity()["Account"]

    function_arn = create_tool_function()
    role_arn = create_execution_role(account, function_arn)

    engine_id = agentcore.create_policy_engine(
        name=ENGINE_NAME, description="Boundary-first sample policy engine"
    )["policyEngineId"]
    engine = wait_for_engine(engine_id)
    print(f"Policy engine {engine_id} is ACTIVE")

    gateway = create_gateway(role_arn, engine["policyEngineArn"], mode)
    gateway_id = gateway["gatewayId"]
    ready = wait_for_gateway(gateway_id)
    gateway_arn = ready.get("gatewayArn") or gateway.get("gatewayArn")
    print(f"Gateway {gateway_id} is READY in {mode} mode")

    caller_arns = create_caller_roles(gateway_arn)
    # The principal id for an assumed role is arn:aws:sts::<account>:assumed-role/<role>.
    agent_principal = f"arn:aws:sts::{account}:assumed-role/{AGENT_ROLE_NAME}"

    # The target comes before the policy, because the policy's action names
    # the tool the target exposes.
    target_id = create_tool_target(gateway_id, function_arn)
    wait_for_target(gateway_id, target_id)
    print(f"Lambda target {TARGET_NAME} ({target_id}) is READY")

    policy_id = create_cedar_policy(engine_id, gateway_arn, agent_principal)
    wait_for_policy(engine_id, policy_id)
    print(f"Cedar policy {policy_id} is ACTIVE against {gateway_arn}")

    callers = {name: assume(arn) for name, arn in caller_arns.items()}
    print("Verifying the policy against live tool calls:")
    passed = verify(ready["gatewayUrl"], mode, callers)
    print(
        "\nRun with --delete when you are finished to avoid ongoing charges."
    )
    return passed


def _delete_role(name):
    """Detach and delete every policy on a role, then the role itself."""
    try:
        for policy in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])
        for policy_name in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        iam.delete_role(RoleName=name)
        print(f"Deleted role {name}")
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            print(f"Role {name}: {error.response['Error']['Code']}")


def delete():
    """Remove resources in dependency order: target, gateway, policy, engine,
    function, roles."""
    gateways = [
        g
        for g in agentcore.list_gateways().get("items", [])
        if g.get("name") == GATEWAY_NAME
    ]
    for gateway in gateways:
        gateway_id = gateway["gatewayId"]
        targets = agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get(
            "items", []
        )
        for target in targets:
            _retry(
                lambda: agentcore.delete_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=target["targetId"]
                ),
                f"target {target['targetId']}",
            )
        _retry(
            lambda: agentcore.delete_gateway(gatewayIdentifier=gateway_id),
            f"gateway {gateway_id}",
        )

    engines = [
        e
        for e in agentcore.list_policy_engines().get("policyEngines", [])
        if e.get("name") == ENGINE_NAME
    ]
    for engine in engines:
        engine_id = engine["policyEngineId"]
        policies = agentcore.list_policies(policyEngineId=engine_id).get("policies", [])
        for policy in policies:
            policy_id = policy["policyId"]
            _retry(
                lambda: agentcore.delete_policy(
                    policyEngineId=engine_id, policyId=policy_id
                ),
                f"policy {policy_id}",
            )
        _retry(
            lambda: agentcore.delete_policy_engine(policyEngineId=engine_id),
            f"policy engine {engine_id}",
        )

    _retry(
        lambda: awslambda.delete_function(FunctionName=FUNCTION_NAME),
        f"function {FUNCTION_NAME}",
    )
    logs = boto3.client("logs", region_name=REGION)
    _retry(
        lambda: logs.delete_log_group(logGroupName=f"/aws/lambda/{FUNCTION_NAME}"),
        f"log group /aws/lambda/{FUNCTION_NAME}",
    )
    _delete_role(GATEWAY_ROLE_NAME)
    _delete_role(FUNCTION_ROLE_NAME)
    _delete_role(AGENT_ROLE_NAME)
    _delete_role(OTHER_ROLE_NAME)


def _retry(operation, label, attempts=8, delay=8):
    """Resources can race their own just-created transient state."""
    for attempt in range(attempts):
        try:
            operation()
            print(f"Deleted {label}")
            return
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return
            print(f"  retry {attempt} on {label}: {code}")
            time.sleep(delay)
    print(f"  gave up on {label}, delete it manually")


def main():
    # A partial earlier run leaves named resources behind, so the next create
    # call fails. IAM reports EntityAlreadyExists, and AgentCore reports a conflict.
    LEFTOVER_ERRORS = {"EntityAlreadyExists", "ConflictException"}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["LOG_ONLY", "ENFORCE"],
        default="ENFORCE",
        help="policy engine mode. Start with LOG_ONLY to review traces first.",
    )
    parser.add_argument("--delete", action="store_true", help="remove all resources")
    args = parser.parse_args()

    if args.delete:
        delete()
        return 0
    try:
        passed = setup(args.mode)
    except ClientError as error:
        if error.response["Error"]["Code"] not in LEFTOVER_ERRORS:
            raise
        print(f"Resources from an earlier run exist ({error.response['Error']['Message']}).")
        print("Run setup_gateway.py --delete, then run setup again.")
        return 1
    if passed:
        print("The policy allowed and denied exactly as written.")
        return 0
    print("The policy did not behave as written. Investigate before relying on it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

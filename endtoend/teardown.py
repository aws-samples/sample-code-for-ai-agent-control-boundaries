# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delete the entire end-to-end stack.

Resources are removed in dependency order and the run ends with a sweep that
reports anything still standing. Every resource carries the bfa-e2e prefix.

Usage:
    python endtoend/teardown.py
    python endtoend/teardown.py --dry-run
"""

import argparse
import os
import time

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
PREFIX = "bfa-e2e"
GATEWAY_NAME = "bfae2egw"
ENGINE_NAME = "bfa_e2e_policy_engine"

sfn = boto3.client("stepfunctions", region_name=REGION)
awslambda = boto3.client("lambda", region_name=REGION)
iam = boto3.client("iam")
sns = boto3.client("sns", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
dynamodb = boto3.client("dynamodb", region_name=REGION)
bedrock = boto3.client("bedrock", region_name=REGION)
cloudwatch = boto3.client("cloudwatch", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
kms = boto3.client("kms", region_name=REGION)
REVIEW_KEY_ALIAS = f"alias/{PREFIX}-review-topic"
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)


def attempt(label, operation, dry_run):
    if dry_run:
        print(f"  would delete {label}")
        return
    try:
        operation()
        print(f"  deleted {label}")
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "NoSuchEntity", "NotFoundException",
                    "ResourceNotFound", "AWS.SimpleQueueService.NonExistentQueue"):
            return
        print(f"  could not delete {label}: {code}")
    except Exception as error:  # noqa: BLE001 - teardown must never abort
        print(f"  could not delete {label}: {type(error).__name__}")


def teardown_executions(dry_run):
    """Stop running executions first. A waitForTaskToken state can hang for an hour."""
    for machine in sfn.list_state_machines().get("stateMachines", []):
        if not machine["name"].startswith(PREFIX):
            continue
        for execution in sfn.list_executions(
            stateMachineArn=machine["stateMachineArn"], statusFilter="RUNNING"
        ).get("executions", []):
            attempt(
                f"execution {execution['name']}",
                lambda arn=execution["executionArn"]: sfn.stop_execution(executionArn=arn),
                dry_run,
            )


def teardown_state_machines(dry_run):
    for machine in sfn.list_state_machines().get("stateMachines", []):
        if machine["name"].startswith(PREFIX):
            attempt(
                f"state machine {machine['name']}",
                lambda arn=machine["stateMachineArn"]: sfn.delete_state_machine(
                    stateMachineArn=arn
                ),
                dry_run,
            )


def teardown_functions(dry_run):
    paginator = awslambda.get_paginator("list_functions")
    for page in paginator.paginate():
        for function in page.get("Functions", []):
            if function["FunctionName"].startswith(PREFIX):
                attempt(
                    f"function {function['FunctionName']}",
                    lambda name=function["FunctionName"]: awslambda.delete_function(
                        FunctionName=name
                    ),
                    dry_run,
                )


def teardown_gateway(dry_run):
    for gateway in agentcore.list_gateways().get("items", []):
        if gateway.get("name") != GATEWAY_NAME:
            continue
        gateway_id = gateway["gatewayId"]
        for target in agentcore.list_gateway_targets(
            gatewayIdentifier=gateway_id
        ).get("items", []):
            attempt(
                f"gateway target {target['targetId']}",
                lambda t=target["targetId"]: agentcore.delete_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=t
                ),
                dry_run,
            )
        if not dry_run:
            time.sleep(5)
        for _ in range(8):
            try:
                if dry_run:
                    print(f"  would delete gateway {gateway_id}")
                    break
                agentcore.delete_gateway(gatewayIdentifier=gateway_id)
                print(f"  deleted gateway {gateway_id}")
                break
            except ClientError as error:
                print(f"  gateway retry: {error.response['Error']['Code']}")
                time.sleep(8)

    for engine in agentcore.list_policy_engines().get("policyEngines", []):
        if engine.get("name") != ENGINE_NAME:
            continue
        engine_id = engine["policyEngineId"]
        for policy in agentcore.list_policies(policyEngineId=engine_id).get(
            "policies", []
        ):
            attempt(
                f"policy {policy['policyId']}",
                lambda p=policy["policyId"]: agentcore.delete_policy(
                    policyEngineId=engine_id, policyId=p
                ),
                dry_run,
            )
        for _ in range(8):
            try:
                if dry_run:
                    print(f"  would delete policy engine {engine_id}")
                    break
                agentcore.delete_policy_engine(policyEngineId=engine_id)
                print(f"  deleted policy engine {engine_id}")
                break
            except ClientError as error:
                print(f"  engine retry: {error.response['Error']['Code']}")
                time.sleep(6)


def teardown_messaging(dry_run):
    for topic in sns.list_topics().get("Topics", []):
        if PREFIX in topic["TopicArn"]:
            attempt(
                f"topic {topic['TopicArn'].rsplit(':', 1)[-1]}",
                lambda arn=topic["TopicArn"]: sns.delete_topic(TopicArn=arn),
                dry_run,
            )
    queues = sqs.list_queues(QueueNamePrefix=PREFIX).get("QueueUrls", []) or []
    for url in queues:
        attempt(f"queue {url.rsplit('/', 1)[-1]}",
                lambda u=url: sqs.delete_queue(QueueUrl=u), dry_run)
    if queues and not dry_run:
        # SQS deletion is eventually consistent and can take up to 60 seconds,
        # so the sweep below may still list the queue on the first pass. That is
        # expected, not a leak. Re-run teardown to confirm.
        print("  note: SQS deletion takes up to 60s to propagate")


def teardown_tables(dry_run):
    for name in dynamodb.list_tables().get("TableNames", []):
        if name.startswith(PREFIX):
            attempt(f"table {name}",
                    lambda n=name: dynamodb.delete_table(TableName=n), dry_run)


def teardown_guardrails(dry_run):
    for guardrail in bedrock.list_guardrails().get("guardrails", []):
        if guardrail.get("name", "").startswith(PREFIX):
            attempt(
                f"guardrail {guardrail['name']}",
                lambda i=guardrail["id"]: bedrock.delete_guardrail(guardrailIdentifier=i),
                dry_run,
            )


def teardown_alarms(dry_run):
    names = [
        a["AlarmName"]
        for a in cloudwatch.describe_alarms(AlarmNamePrefix=PREFIX).get(
            "MetricAlarms", []
        )
    ]
    if names:
        attempt(f"alarms {names}",
                lambda: cloudwatch.delete_alarms(AlarmNames=names), dry_run)


def teardown_roles(dry_run):
    for page in iam.get_paginator("list_roles").paginate():
        for role in page.get("Roles", []):
            name = role["RoleName"]
            if not name.startswith(PREFIX):
                continue
            for policy in iam.list_attached_role_policies(RoleName=name).get(
                "AttachedPolicies", []
            ):
                attempt(
                    f"detach {policy['PolicyName']} from {name}",
                    lambda n=name, a=policy["PolicyArn"]: iam.detach_role_policy(
                        RoleName=n, PolicyArn=a
                    ),
                    dry_run,
                )
            for policy_name in iam.list_role_policies(RoleName=name).get(
                "PolicyNames", []
            ):
                attempt(
                    f"inline {policy_name} on {name}",
                    lambda n=name, p=policy_name: iam.delete_role_policy(
                        RoleName=n, PolicyName=p
                    ),
                    dry_run,
                )
            attempt(f"role {name}",
                    lambda n=name: iam.delete_role(RoleName=n), dry_run)


def teardown_log_groups(dry_run):
    for group in logs.describe_log_groups(
        logGroupNamePrefix="/aws/vendedlogs/states/bfa-e2e"
    ).get("logGroups", []):
        attempt(
            f"log group {group['logGroupName']}",
            lambda n=group["logGroupName"]: logs.delete_log_group(logGroupName=n),
            dry_run,
        )
    for prefix in ("/aws/lambda/bfa-e2e",):
        for group in logs.describe_log_groups(logGroupNamePrefix=prefix).get(
            "logGroups", []
        ):
            attempt(
                f"log group {group['logGroupName']}",
                lambda n=group["logGroupName"]: logs.delete_log_group(logGroupName=n),
                dry_run,
            )


def teardown_review_key(dry_run):
    """Remove the alias and schedule the review-topic key for deletion.

    AWS KMS never deletes a key immediately. Seven days is the minimum waiting
    period. Cancel with kms cancel-key-deletion if the key is still needed.
    """
    try:
        key_id = kms.describe_key(KeyId=REVIEW_KEY_ALIAS)["KeyMetadata"]["KeyId"]
    except ClientError:
        return
    attempt(f"alias {REVIEW_KEY_ALIAS}",
            lambda: kms.delete_alias(AliasName=REVIEW_KEY_ALIAS), dry_run)
    attempt(f"key {key_id} (scheduled, 7-day waiting period)",
            lambda: kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=7),
            dry_run)


def sweep():
    """Report anything left. A partially deleted gateway keeps costing money."""
    print("\n=== SWEEP ===")
    checks = {
        "state machines": lambda: [
            m["name"]
            for m in sfn.list_state_machines().get("stateMachines", [])
            if m["name"].startswith(PREFIX)
        ],
        "functions": lambda: [
            f["FunctionName"]
            for p in awslambda.get_paginator("list_functions").paginate()
            for f in p.get("Functions", [])
            if f["FunctionName"].startswith(PREFIX)
        ],
        "gateways": lambda: [
            g["name"]
            for g in agentcore.list_gateways().get("items", [])
            if g.get("name") == GATEWAY_NAME
        ],
        "policy engines": lambda: [
            e["name"]
            for e in agentcore.list_policy_engines().get("policyEngines", [])
            if e.get("name") == ENGINE_NAME
        ],
        "tables": lambda: [
            t for t in dynamodb.list_tables().get("TableNames", [])
            if t.startswith(PREFIX)
        ],
        "guardrails": lambda: [
            g["name"] for g in bedrock.list_guardrails().get("guardrails", [])
            if g.get("name", "").startswith(PREFIX)
        ],
        "topics": lambda: [
            t["TopicArn"].rsplit(":", 1)[-1]
            for t in sns.list_topics().get("Topics", [])
            if PREFIX in t["TopicArn"]
        ],
        "queues": lambda: sqs.list_queues(QueueNamePrefix=PREFIX).get("QueueUrls", []) or [],
        "key aliases": lambda: [
            a["AliasName"]
            for p in kms.get_paginator("list_aliases").paginate()
            for a in p.get("Aliases", [])
            if a["AliasName"] == REVIEW_KEY_ALIAS
        ],
        "roles": lambda: [
            r["RoleName"]
            for p in iam.get_paginator("list_roles").paginate()
            for r in p.get("Roles", [])
            if r["RoleName"].startswith(PREFIX)
        ],
    }
    leftovers = 0
    for label, check in checks.items():
        try:
            found = check()
        except Exception as error:  # noqa: BLE001
            print(f"  {label}: could not check ({type(error).__name__})")
            continue
        leftovers += len(found)
        print(f"  {label}: {found or 'NONE'}")
    print(f"\n{'CLEAN' if leftovers == 0 else f'{leftovers} RESOURCE(S) REMAIN'}")
    return leftovers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Tearing down {PREFIX}-* in {REGION}\n")
    for label, step in (
        ("running executions", teardown_executions),
        ("state machines", teardown_state_machines),
        ("gateway and policy engine", teardown_gateway),
        ("functions", teardown_functions),
        ("messaging", teardown_messaging),
        ("review topic key", teardown_review_key),
        ("tables", teardown_tables),
        ("guardrails", teardown_guardrails),
        ("alarms", teardown_alarms),
        ("roles", teardown_roles),
        ("log groups", teardown_log_groups),
    ):
        print(f"{label}:")
        try:
            step(args.dry_run)
        except Exception as error:  # noqa: BLE001
            print(f"  step failed: {type(error).__name__}: {error}")

    if args.dry_run:
        sweep()
        return
    # State machine and SQS deletions are asynchronous and can take up to about
    # 60 seconds to disappear from list calls. Re-sweep until clean or out of time,
    # so a resource that is still deleting is not reported as a leak.
    for attempt_number in range(8):
        time.sleep(15 if attempt_number else 8)
        if sweep() == 0:
            return
        print("  waiting for asynchronous deletions to finish...")
    print("Resources above still exist after two minutes. Delete them manually.")


if __name__ == "__main__":
    main()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delete every resource the samples create.

All samples name their resources with the bfa-sample prefix, so this removes
them in dependency order. Review the output and confirm nothing remains in the
console, because a partially deleted gateway keeps accruing charges.

Usage:
    python cleanup.py
    python cleanup.py --dry-run
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
PREFIX = "bfa-sample"
GATEWAY_PREFIX = "bfasample"

REPO_ROOT = Path(__file__).parent


def _load_module(path, name):
    """Import a sibling sample module by path so it can be called directly."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def find_guardrails(dry_run):
    bedrock = boto3.client("bedrock", region_name=REGION)
    found = []
    for guardrail in bedrock.list_guardrails().get("guardrails", []):
        if guardrail.get("name", "").startswith(PREFIX):
            found.append(guardrail["id"])
            if not dry_run:
                bedrock.delete_guardrail(guardrailIdentifier=guardrail["id"])
    return found


def find_state_machines(dry_run):
    sfn = boto3.client("stepfunctions", region_name=REGION)
    found = []
    for machine in sfn.list_state_machines().get("stateMachines", []):
        if machine.get("name", "").startswith(PREFIX):
            found.append(machine["stateMachineArn"])
            if not dry_run:
                sfn.delete_state_machine(stateMachineArn=machine["stateMachineArn"])
    return found


def find_alarms(dry_run):
    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    names = [
        alarm["AlarmName"]
        for alarm in cloudwatch.describe_alarms(AlarmNamePrefix=PREFIX).get(
            "MetricAlarms", []
        )
    ]
    if names and not dry_run:
        cloudwatch.delete_alarms(AlarmNames=names)
    return names


def find_log_groups(dry_run):
    """The control-safety sample's execution log group, which deleting the
    state machine leaves behind."""
    logs = boto3.client("logs", region_name=REGION)
    names = [
        group["logGroupName"]
        for group in logs.describe_log_groups(
            logGroupNamePrefix=f"/aws/vendedlogs/states/{PREFIX}"
        ).get("logGroups", [])
    ]
    if not dry_run:
        for name in names:
            logs.delete_log_group(logGroupName=name)
    return names


def find_gateway_resources():
    """Gateway resources are deleted by setup_gateway.py, which handles ordering.

    The tool function is listed too, so a run that failed after creating it but
    before creating the gateway is still cleaned up.
    """
    agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
    found = [
        f"gateway {gateway.get('name')}"
        for gateway in agentcore.list_gateways().get("items", [])
        if str(gateway.get("name", "")).startswith(GATEWAY_PREFIX)
    ]
    awslambda = boto3.client("lambda", region_name=REGION)
    for page in awslambda.get_paginator("list_functions").paginate():
        found += [
            f"function {function['FunctionName']}"
            for function in page["Functions"]
            if function["FunctionName"].startswith(PREFIX)
        ]
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be deleted"
    )
    args = parser.parse_args()
    verb = "Would delete" if args.dry_run else "Deleted"

    for label, finder in (
        ("guardrails", find_guardrails),
        ("state machines", find_state_machines),
        ("alarms", find_alarms),
        ("log groups", find_log_groups),
    ):
        try:
            found = finder(args.dry_run)
            print(f"{verb} {len(found)} {label}: {found or 'none'}")
        except ClientError as error:
            print(f"Could not process {label}: {error.response['Error']['Code']}")

    try:
        gateway_resources = find_gateway_resources()
    except ClientError as error:
        print(f"Could not list gateway resources: {error.response['Error']['Code']}")
        return

    print(
        f"{'Would delete' if args.dry_run else 'Deleting'} "
        f"{len(gateway_resources)} gateway sample resources: "
        f"{gateway_resources or 'none'}"
    )
    if args.dry_run:
        return

    # Delegate to setup_gateway.delete(), which knows the required delete ordering
    # (target, gateway, policy, engine, function, roles) and retries transient
    # states. It also removes the two IAM roles, which this script cannot list
    # by prefix cheaply. Safe to run when nothing exists.
    setup_gateway = _load_module(
        REPO_ROOT / "gateway-authorization" / "setup_gateway.py", "setup_gateway"
    )
    setup_gateway.delete()


if __name__ == "__main__":
    main()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validate and optionally deploy the boundary-first state machine.

The definition in state_machine.asl.json uses ${...} placeholders for the
resource ARNs that vary per environment. This script substitutes them from
environment variables, validates the result, and can deploy it.

Validation makes no billable resources and needs no IAM role, so run it first.

Usage:
    python control-safety/deploy_state_machine.py
    python control-safety/deploy_state_machine.py --deploy --role-arn <execution-role-arn>

The execution role needs more than Lambda invoke and Bedrock permissions.
Execution logging and X-Ray tracing are both enabled here, because the audit
trail depends on them, and each needs its own grants:

  - logs:CreateLogDelivery, logs:GetLogDelivery, logs:UpdateLogDelivery,
    logs:DeleteLogDelivery, logs:ListLogDeliveries, logs:PutResourcePolicy,
    logs:DescribeResourcePolicies, logs:DescribeLogGroups
  - xray:PutTraceSegments, xray:PutTelemetryRecords, xray:GetSamplingRules,
    xray:GetSamplingTargets

Environment variables:
    AWS_REGION                    default us-west-2
    BEDROCK_MODEL_ID              model id for the agent tasks
    CLASSIFY_FUNCTION_ARN         Lambda ARN for intent classification
    VALIDATE_FUNCTION_ARN         Lambda ARN for the intermediate validator
    HUMAN_REVIEW_TOPIC_ARN        SNS topic ARN for the safety boundary
"""

import argparse
import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
STATE_MACHINE_NAME = "bfa-sample-boundary-sm"
DEFINITION_PATH = Path(__file__).parent / "state_machine.asl.json"

# The /aws/vendedlogs/ prefix is the documented convention for service-delivered
# logs and avoids consuming the account's CloudWatch Logs resource policy limit.
LOG_GROUP_NAME = f"/aws/vendedlogs/states/{STATE_MACHINE_NAME}"

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5"


def build_definition(account_id):
    """Substitute environment-specific ARNs into the definition template."""
    prefix = f"arn:aws:lambda:{REGION}:{account_id}:function"
    substitutions = {
        "${BedrockModelId}": os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        "${ClassifyIntentFunctionArn}": os.environ.get(
            "CLASSIFY_FUNCTION_ARN", f"{prefix}:bfa-sample-classify-intent"
        ),
        "${ValidateOutputFunctionArn}": os.environ.get(
            "VALIDATE_FUNCTION_ARN", f"{prefix}:bfa-sample-validate-output"
        ),
        "${HumanReviewTopicArn}": os.environ.get(
            "HUMAN_REVIEW_TOPIC_ARN",
            f"arn:aws:sns:{REGION}:{account_id}:bfa-sample-human-review",
        ),
    }

    definition = DEFINITION_PATH.read_text()
    for placeholder, value in substitutions.items():
        definition = definition.replace(placeholder, value)

    remaining = [line for line in definition.splitlines() if "${" in line]
    if remaining:
        raise ValueError(f"Unsubstituted placeholders remain: {remaining}")

    return definition


def ensure_log_group():
    """Create the execution log group and return an ARN Step Functions accepts.

    Step Functions requires loggingConfiguration.destinations whenever level is
    not OFF, and the log group ARN it expects ends with ':*'.
    """
    logs = boto3.client("logs", region_name=REGION)
    try:
        logs.create_log_group(logGroupName=LOG_GROUP_NAME)
        print(f"Created log group {LOG_GROUP_NAME}")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        print(f"Reusing log group {LOG_GROUP_NAME}")

    described = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP_NAME)
    for group in described.get("logGroups", []):
        if group["logGroupName"] == LOG_GROUP_NAME:
            arn = group["arn"]
            return arn if arn.endswith(":*") else f"{arn}:*"
    raise RuntimeError(f"Could not resolve ARN for {LOG_GROUP_NAME}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="create the state machine after validation",
    )
    parser.add_argument(
        "--role-arn",
        help="execution role ARN, required with --deploy",
    )
    parser.add_argument(
        "--log-group-arn",
        help="existing log group ARN. Omit to create one automatically.",
    )
    args = parser.parse_args()

    if args.deploy and not args.role_arn:
        parser.error("--deploy requires --role-arn")

    account_id = boto3.client("sts").get_caller_identity()["Account"]
    definition = build_definition(account_id)

    sfn = boto3.client("stepfunctions", region_name=REGION)
    validation = sfn.validate_state_machine_definition(
        definition=definition, type="STANDARD"
    )
    print(f"Validation result: {validation['result']}")
    for diagnostic in validation.get("diagnostics", []):
        print(f"  {diagnostic['severity']}: {diagnostic['message']}")

    if validation["result"] != "OK":
        raise SystemExit("Definition did not validate. Fix the diagnostics above.")

    # Confirm the resilience and privacy properties described in the post are present.
    states = json.loads(definition)["States"]
    for name in ("HousingAgent", "FoodAssistanceAgent"):
        assert "Retry" in states[name], f"{name} is missing Retry"
        assert "Catch" in states[name], f"{name} is missing Catch"
    message = states["FallbackHumanReview"]["Parameters"]["Message"]
    assert "caseId.$" in message, "human review payload is missing caseId"
    assert "input.$" not in message, "human review payload leaks the full record"
    print("Retry, Catch, and a minimized human-review payload are all present.")

    if not args.deploy:
        print("\nValidation only. Re-run with --deploy --role-arn <arn> to create it.")
        return

    log_group_arn = args.log_group_arn or ensure_log_group()

    created = sfn.create_state_machine(
        name=STATE_MACHINE_NAME,
        definition=definition,
        roleArn=args.role_arn,
        type="STANDARD",
        # Turn on logging and tracing so the execution history the audit
        # depends on is captured and queryable.
        loggingConfiguration={
            "level": "ALL",
            "includeExecutionData": True,
            "destinations": [
                {"cloudWatchLogsLogGroup": {"logGroupArn": log_group_arn}}
            ],
        },
        tracingConfiguration={"enabled": True},
    )
    print(f"Created {created['stateMachineArn']}")
    print("Run cleanup.py when you are finished to avoid ongoing charges.")


if __name__ == "__main__":
    main()

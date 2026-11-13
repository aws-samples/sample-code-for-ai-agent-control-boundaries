# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The irreversible action, and the audit record that makes it defensible.

It writes the award
to DynamoDB with a conditional put, so the same case cannot be awarded twice
even if the workflow is retried.

Two properties worth copying:
  - The write is conditional, so a retry after a partial failure is safe.
  - The record captures which rule was applied and who approved it, because an
    auditor asks "why was this decided" and "who signed off", not "what did the
    model output".
"""

import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

REGION = os.environ["AWS_REGION"]
TABLE_NAME = os.environ["AWARDS_TABLE"]

dynamodb = boto3.client("dynamodb", region_name=REGION)


def lambda_handler(event, context=None):
    case_id = event["caseId"]
    award = event.get("passthrough") or event.get("agentOutput") or {}
    approver = event.get("approvedBy", "automated")

    item = {
        "caseId": {"S": case_id},
        "awardedAt": {"S": datetime.now(timezone.utc).isoformat()},
        "category": {"S": str(award.get("category"))},
        "amount": {"N": str(award.get("amount"))},
        "sourceCitation": {"S": str(award.get("source_citation"))},
        "approvedBy": {"S": approver},
    }

    try:
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item=item,
            # Idempotency: a replayed execution must not double-award.
            ConditionExpression="attribute_not_exists(caseId)",
        )
        return {"caseId": case_id, "recorded": True, "amount": award.get("amount")}
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {
                "caseId": case_id,
                "recorded": False,
                "reason": "Award already recorded for this case",
            }
        raise

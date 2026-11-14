# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The irreversible write: conditional, and reports a refusal.

Offline, against a fake DynamoDB client. The conditional put is what stops a
replayed execution from paying a case twice, so removing it must fail here.
A refused write must report recorded: false, which the state machine's
CheckRecorded state turns into a failed execution.
"""

import os
import sys
from pathlib import Path

from botocore.exceptions import ClientError

HERE = Path(__file__).parent
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWARDS_TABLE", "test")
sys.path.insert(0, str(HERE / "lambdas"))

import record_award  # noqa: E402

failures = []


def check(ok, label, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        {detail}")
        failures.append(label)


class FakeDynamoDB:
    """Enforces attribute_not_exists(caseId) the way DynamoDB does."""

    def __init__(self):
        self.items = {}
        self.calls = []

    def put_item(self, TableName, Item, ConditionExpression=None):
        self.calls.append(ConditionExpression)
        key = Item["caseId"]["S"]
        if ConditionExpression == "attribute_not_exists(caseId)" and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = Item


class FailingDynamoDB:
    def put_item(self, **kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                     "Message": "slow down"}}, "PutItem")


EVENT = {
    "caseId": "C-TEST",
    "passthrough": {"category": "housing", "amount": 7320, "source_citation": "HA-3.2"},
    "approvedBy": "automated",
}


def main():
    fake = FakeDynamoDB()
    record_award.dynamodb = fake

    first = record_award.lambda_handler(dict(EVENT))
    check(first.get("recorded") is True, "the first write is recorded", first)
    check(fake.calls[0] == "attribute_not_exists(caseId)",
          "the write is conditional on the case not existing", f"ConditionExpression={fake.calls[0]!r}")

    second = record_award.lambda_handler({**EVENT, "passthrough": {**EVENT["passthrough"], "amount": 9000}})
    check(second.get("recorded") is False, "a second write for the same case is refused", second)
    check(fake.items["C-TEST"]["amount"]["N"] == "7320",
          "the refused write leaves the original award unchanged", fake.items["C-TEST"]["amount"])

    record_award.dynamodb = FailingDynamoDB()
    try:
        record_award.lambda_handler(dict(EVENT))
        check(False, "any other DynamoDB error raises, so the Catch escalates it", "returned normally")
    except ClientError:
        check(True, "any other DynamoDB error raises, so the Catch escalates it")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

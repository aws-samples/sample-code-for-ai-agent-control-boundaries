# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda handler behind the record_award gateway tool.

AgentCore Gateway invokes this function only after the Cedar policy has
permitted the call, so the function does not repeat the ceiling check. The
gateway passes the tool arguments as the event and the tool name, prefixed with
the target name, in the client context.

This sample returns a receipt instead of writing to a system of record. The
end-to-end stack in endtoend/ shows the real write, reached only by a validated
award or a human approval.
"""

import json

TOOL_NAME = "record_award"


def lambda_handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    tool = custom.get("bedrockAgentCoreToolName", "")
    # The gateway prefixes tool names with "<targetName>___".
    if tool.split("___")[-1] != TOOL_NAME:
        return {"error": f"unknown tool {tool!r}"}

    receipt = {
        "recorded": True,
        "case_id": event.get("case_id"),
        "program": event.get("program"),
        "amount": event.get("amount"),
    }
    print(json.dumps({"tool": tool, "receipt": receipt}))
    return receipt

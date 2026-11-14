# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Input and output boundaries with Amazon Bedrock Guardrails.

Creates one guardrail that enforces both the input boundary (PII blocking,
prompt-attack filtering) and the output boundary (contextual grounding), then
demonstrates each control against live text.

One guardrail definition can be reused across agents, so coverage does not
depend on each agent's own code.

Usage:
    python input-output/guardrail.py            # create, demonstrate, then delete
    python input-output/guardrail.py --keep     # leave the guardrail in place
"""

import argparse
import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
GUARDRAIL_NAME = "bfa-sample-boundary-guardrail"


def create_guardrail(bedrock):
    """Create the guardrail that implements the input and output boundaries."""
    return bedrock.create_guardrail(
        name=GUARDRAIL_NAME,
        description="Input and output boundary enforcement for an eligibility assistant",
        blockedInputMessaging="Request blocked by boundary guardrail.",
        blockedOutputsMessaging="Response blocked by boundary guardrail.",
        # Input boundary: reject prompt injection hidden in free-text notes.
        contentPolicyConfig={
            "filtersConfig": [
                {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
                {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
            ]
        },
        # Input boundary: keep sensitive identifiers from reaching the model.
        sensitiveInformationPolicyConfig={
            "piiEntitiesConfig": [
                {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
                {"type": "EMAIL", "action": "ANONYMIZE"},
            ]
        },
        # Output boundary: score whether a response is grounded in its source.
        contextualGroundingPolicyConfig={
            "filtersConfig": [
                {"type": "GROUNDING", "threshold": 0.8},
                {"type": "RELEVANCE", "threshold": 0.7},
            ]
        },
    )


def demonstrate_input_boundary(runtime, guardrail_id, version):
    """A Social Security number in the input is blocked before the model sees it."""
    result = runtime.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version,
        source="INPUT",
        content=[{"text": {"text": "My SSN is 123-45-6789."}}],
    )
    print(f"  input boundary (SSN):      action={result['action']}")
    return result


def demonstrate_pii_anonymization(runtime, guardrail_id, version):
    """An email address in the output is anonymized rather than blocked."""
    result = runtime.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version,
        source="OUTPUT",
        content=[{"text": {"text": "Reach me at applicant@example.com."}}],
    )
    outputs = result.get("outputs") or [{}]
    print(f"  output boundary (email):   text={outputs[0].get('text', '')!r}")
    return result


def demonstrate_output_boundary(runtime, guardrail_id, version):
    """An award figure not supported by the source is blocked as ungrounded.

    Returns the action so the caller can assert the filter fired.
    """
    result = runtime.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version,
        source="OUTPUT",
        content=[
            {
                "text": {
                    "text": "The maximum housing award is $2,000 per month.",
                    "qualifiers": ["grounding_source"],
                }
            },
            {
                "text": {
                    "text": "What is the maximum housing award?",
                    "qualifiers": ["query"],
                }
            },
            {"text": {"text": "The maximum housing award is $50,000 per month."}},
        ],
    )
    print(f"  output boundary (grounding): action={result['action']}")
    return result


def demonstrate_grounded_output_passes(runtime, guardrail_id, version):
    """The control group. A grounded answer must NOT be blocked.

    Without this, a filter that blocks everything would look like success.
    """
    result = runtime.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version,
        source="OUTPUT",
        content=[
            {
                "text": {
                    "text": "The maximum housing award is $2,000 per month.",
                    "qualifiers": ["grounding_source"],
                }
            },
            {
                "text": {
                    "text": "What is the maximum housing award?",
                    "qualifiers": ["query"],
                }
            },
            {"text": {"text": "The maximum housing award is $2,000 per month."}},
        ],
    )
    print(f"  output boundary (grounded control): action={result['action']}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not delete the guardrail after the demonstration",
    )
    args = parser.parse_args()

    bedrock = boto3.client("bedrock", region_name=REGION)
    runtime = boto3.client("bedrock-runtime", region_name=REGION)

    created = create_guardrail(bedrock)
    guardrail_id, version = created["guardrailId"], created["version"]
    print(f"Created guardrail {guardrail_id} version {version} in {REGION}")

    failures = []
    try:
        blocked = demonstrate_input_boundary(runtime, guardrail_id, version)
        anonymized = demonstrate_pii_anonymization(runtime, guardrail_id, version)
        ungrounded = demonstrate_output_boundary(runtime, guardrail_id, version)
        grounded = demonstrate_grounded_output_passes(runtime, guardrail_id, version)

        # Each boundary must both fire when
        # it should and stay quiet when it should not.
        if blocked["action"] != "GUARDRAIL_INTERVENED":
            failures.append("SSN in input was not blocked")
        outputs = anonymized.get("outputs") or [{}]
        if "applicant@example.com" in outputs[0].get("text", ""):
            failures.append("email was not anonymized")
        if ungrounded["action"] != "GUARDRAIL_INTERVENED":
            failures.append("ungrounded output was not blocked")
        if grounded["action"] == "GUARDRAIL_INTERVENED":
            failures.append("grounded output was blocked, so the filter is too strict")

        print()
        if failures:
            for failure in failures:
                print(f"  FAIL {failure}")
        else:
            print("  4/4 boundary behaviors confirmed")

        print(
            "\nTo enforce this guardrail on a model call, pass guardrailIdentifier "
            "and guardrailVersion to InvokeModel or Converse."
        )
    finally:
        if args.keep:
            print(f"\nGuardrail {guardrail_id} left in place. Delete it when finished.")
        else:
            bedrock.delete_guardrail(guardrailIdentifier=guardrail_id)
            print(f"\nDeleted guardrail {guardrail_id}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

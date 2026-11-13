# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail fast when a configured model is no longer usable.

Bedrock model IDs move from ACTIVE to LEGACY to end of life. A LEGACY model
you have not used in 30 days is denied with:

    ResourceNotFoundException: Access denied. This Model is marked by provider
    as Legacy and you have not been actively using the model in the last 30
    days. Please upgrade to an active model on Amazon Bedrock

An inference profile's own status does not show this. Both of these profiles
report ACTIVE while the model behind them cannot be relied on, and the second
cannot be invoked at all:

    us.anthropic.claude-sonnet-4-20250514-v1:0
        profile ACTIVE, foundation model LEGACY

    us.anthropic.claude-3-haiku-20240307-v1:0
        profile ACTIVE, foundation model past end of life. get_foundation_model
        raises ResourceNotFoundException and the id is gone from
        list_foundation_models entirely.

Check the foundation model, not the profile, and treat an ID missing from the
catalogue as retired. Call assert_model_usable() at startup to fail with a list
of ACTIVE alternatives.
"""

import os

import boto3

# Cross-Region inference profile prefixes. A profile id is the foundation model
# id with one of these prepended.
PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")


def to_foundation_model_id(model_id):
    """Strip any cross-Region inference profile prefix."""
    for prefix in PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def _catalog(region):
    bedrock = boto3.client("bedrock", region_name=region)
    catalog = {}
    for summary in bedrock.list_foundation_models().get("modelSummaries", []):
        lifecycle = (summary.get("modelLifecycle") or {}).get("status", "UNKNOWN")
        catalog[summary["modelId"]] = {
            "status": lifecycle,
            "provider": summary.get("providerName", ""),
        }
    return catalog


def active_alternatives(model_id, region, limit=6):
    """Suggest ACTIVE models from the same provider, newest-looking last."""
    foundation_id = to_foundation_model_id(model_id)
    family = foundation_id.split(".")[0] if "." in foundation_id else ""
    catalog = _catalog(region)
    prefix = next(
        (p for p in PROFILE_PREFIXES if model_id.startswith(p)), ""
    )
    candidates = sorted(
        f"{prefix}{mid}"
        for mid, meta in catalog.items()
        if meta["status"] == "ACTIVE" and mid.startswith(f"{family}.")
    )
    return candidates[-limit:]


def assert_model_usable(model_id, region=None):
    """Raise with an actionable message unless the model is ACTIVE.

    Returns the model_id unchanged so this can wrap a default inline.
    """
    region = region or os.environ.get("AWS_REGION", "us-west-2")
    foundation_id = to_foundation_model_id(model_id)
    catalog = _catalog(region)
    entry = catalog.get(foundation_id)

    if entry is None:
        raise SystemExit(
            f"Model {model_id!r} is not available in {region}. Its foundation "
            f"model id {foundation_id!r} does not appear in "
            f"list_foundation_models, which usually means it was retired.\n"
            f"Set BEDROCK_MODEL_ID to one of:\n  "
            + "\n  ".join(active_alternatives(model_id, region))
        )

    if entry["status"] != "ACTIVE":
        raise SystemExit(
            f"Model {model_id!r} has foundation model lifecycle status "
            f"{entry['status']} in {region}. A LEGACY model can still invoke "
            f"for a while and then begin denying requests without notice.\n"
            f"Set BEDROCK_MODEL_ID to one of:\n  "
            + "\n  ".join(active_alternatives(model_id, region))
        )

    return model_id


def report(region=None):
    """Print the ACTIVE and LEGACY split. Useful before pinning a default."""
    region = region or os.environ.get("AWS_REGION", "us-west-2")
    catalog = _catalog(region)
    for status in ("ACTIVE", "LEGACY"):
        listed = sorted(
            mid for mid, meta in catalog.items() if meta["status"] == status
        )
        print(f"{status} ({len(listed)}):")
        for mid in listed:
            print(f"  {mid}")


if __name__ == "__main__":
    report()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deploy the complete boundary-first stack.

Creates every boundary as a real resource, not an illustration:

    DynamoDB table          the irreversible action's system of record
    Bedrock guardrail       boundaries 1 and 3, input screening and grounding
    4 Lambda functions      classify, compute award in code, validate, record
    SNS topic + SQS queue   boundary 5, the human approval channel
    Step Functions          boundary 4, deterministic routing, with logging on
    CloudWatch alarm        boundary violation rate

Run endtoend/teardown.py when finished. Everything is prefixed bfa-e2e.

Usage:
    python endtoend/deploy.py
"""

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "common"))
from model_lifecycle import assert_model_usable  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-west-2")

# Validated before anything is deployed. A LEGACY model would otherwise deploy
# cleanly and fail at the first execution.
MODEL_ID = assert_model_usable(
    os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5"), REGION
)
PREFIX = "bfa-e2e"
RUNTIME = "python3.12"

ACCOUNT = boto3.client("sts").get_caller_identity()["Account"]
iam = boto3.client("iam")
awslambda = boto3.client("lambda", region_name=REGION)
bedrock = boto3.client("bedrock", region_name=REGION)
dynamodb = boto3.client("dynamodb", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
cloudwatch = boto3.client("cloudwatch", region_name=REGION)
kms = boto3.client("kms", region_name=REGION)

TABLE = f"{PREFIX}-awards"
REVIEW_KEY_ALIAS = f"alias/{PREFIX}-review-topic"
LOG_RETENTION_DAYS = 30
TOPIC = f"{PREFIX}-human-review"
QUEUE = f"{PREFIX}-human-review-queue"
STATE_MACHINE = f"{PREFIX}-boundary-sm"
LOG_GROUP = f"/aws/vendedlogs/states/{STATE_MACHINE}"
ALARM = f"{PREFIX}-BoundaryViolationRate-High"
SFN_ROLE = f"{PREFIX}-sfn-role"

# Each function gets only the actions its own code calls. Resources are scoped
# where it matters for the boundary: only record-award can write to the awards
# table, so the model-calling function cannot perform the irreversible write
# even if its code changed. The model grant names the deployed model, the log
# grant names the stack's function log groups, and metrics are limited to the
# boundary namespace.
CLASSIFY_ROLE = f"{PREFIX}-classify-role"
METRICS_ROLE = f"{PREFIX}-metrics-role"
RECORD_ROLE = f"{PREFIX}-record-role"

FUNCTIONS = {
    f"{PREFIX}-classify": ("classify.py", 60, CLASSIFY_ROLE),
    f"{PREFIX}-compute-award": ("compute_award.py", 15, METRICS_ROLE),
    f"{PREFIX}-validate-output": ("validate_output.py", 15, METRICS_ROLE),
    f"{PREFIX}-record-award": ("record_award.py", 15, RECORD_ROLE),
}


def model_resource_arns(model_id):
    """The ARNs Bedrock authorizes for a call to model_id.

    An inference profile is checked against the profile and against every
    foundation model it routes to, in each Region it routes to, so all of
    them are granted and nothing else.
    """
    try:
        profile = bedrock.get_inference_profile(inferenceProfileIdentifier=model_id)
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("ResourceNotFoundException", "ValidationException"):
            raise
        return [f"arn:aws:bedrock:{REGION}::foundation-model/{model_id}"]
    return [profile["inferenceProfileArn"]] + [m["modelArn"] for m in profile["models"]]


def assume_role_policy(service):
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": service},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def ensure_role(name, service, inline):
    try:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=assume_role_policy(service),
            Description="Boundary-first end-to-end sample",
        )["Role"]["Arn"]
        print(f"  created role {name}")
    except ClientError as error:
        if error.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  reusing role {name}")
    iam.put_role_policy(
        RoleName=name, PolicyName="permissions", PolicyDocument=json.dumps(inline)
    )
    return arn


def zip_source(filename):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "lambda_function.py", (HERE / "lambdas" / filename).read_text()
        )
    return buffer.getvalue()


def create_table():
    try:
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "caseId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "caseId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
            SSESpecification={"Enabled": True},
        )
        print(f"  created table {TABLE}")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise
        print(f"  reusing table {TABLE}")
    dynamodb.get_waiter("table_exists").wait(TableName=TABLE)


GUARDRAIL_CONFIG = {
    "description": "Input and output boundary enforcement",
    "blockedInputMessaging": "Request blocked by boundary guardrail.",
    "blockedOutputsMessaging": "Response blocked by boundary guardrail.",
    "contentPolicyConfig": {
        "filtersConfig": [
            {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
            {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        ]
    },
    "sensitiveInformationPolicyConfig": {
        "piiEntitiesConfig": [
            {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
            {"type": "EMAIL", "action": "ANONYMIZE"},
        ]
    },
    # No contextual grounding here. This stack's output is extracted JSON, and
    # grounding scores correct extractions as ungrounded. classify.py checks
    # the extracted numbers against the record in code instead.
}


def create_guardrail():
    name = f"{PREFIX}-boundary-guardrail"
    existing = [
        g for g in bedrock.list_guardrails().get("guardrails", []) if g.get("name") == name
    ]
    if existing:
        # Apply the current configuration rather than silently reusing a stale one.
        guardrail_id = existing[0]["id"]
        bedrock.update_guardrail(guardrailIdentifier=guardrail_id, name=name, **GUARDRAIL_CONFIG)
        print(f"  updated guardrail {guardrail_id}")
        return guardrail_id, "DRAFT"
    created = bedrock.create_guardrail(name=name, **GUARDRAIL_CONFIG)
    print(f"  created guardrail {created['guardrailId']}")
    return created["guardrailId"], created["version"]


def ensure_review_key():
    """Customer managed key for the review topic, as the post advises.

    The publisher (the Step Functions role) is granted use of the key through
    its IAM policy, which the first key-policy statement enables.
    """
    try:
        key_arn = kms.describe_key(KeyId=REVIEW_KEY_ALIAS)["KeyMetadata"]["Arn"]
        print(f"  reusing key {REVIEW_KEY_ALIAS}")
        return key_arn
    except ClientError as error:
        if error.response["Error"]["Code"] != "NotFoundException":
            raise
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                # Lets IAM policies in this account grant use of the key, which
                # is how the Step Functions role gets permission to publish.
                "Sid": "EnableIamPolicies",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
                "Action": "kms:*",
                # In a key policy, "*" means this key only.
                "Resource": "*",
            },
        ],
    }
    key_arn = kms.create_key(
        Description="Boundary-first end-to-end sample: human-review topic",
        Policy=json.dumps(policy),
    )["KeyMetadata"]["Arn"]
    kms.create_alias(AliasName=REVIEW_KEY_ALIAS, TargetKeyId=key_arn)
    print(f"  created key {REVIEW_KEY_ALIAS}")
    return key_arn


def create_messaging(key_arn):
    topic_arn = sns.create_topic(Name=TOPIC)["TopicArn"]
    # Encrypt the review topic: escalations carry a case reference and a task
    # token that can resume a paused execution.
    sns.set_topic_attributes(
        TopicArn=topic_arn, AttributeName="KmsMasterKeyId", AttributeValue=key_arn
    )
    queue_url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    sqs.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={
            "Policy": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "sns.amazonaws.com"},
                            "Action": "sqs:SendMessage",
                            "Resource": queue_arn,
                            "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}},
                        }
                    ],
                }
            )
        },
    )
    subscriptions = sns.list_subscriptions_by_topic(TopicArn=topic_arn).get(
        "Subscriptions", []
    )
    if not any(s["Endpoint"] == queue_arn for s in subscriptions):
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)
    print(f"  created topic {TOPIC} with queue {QUEUE} subscribed")
    return topic_arn, queue_url


def create_functions(role_arns, guardrail_id, guardrail_version):
    arns = {}
    for name, (source, timeout, role_name) in FUNCTIONS.items():
        role_arn = role_arns[role_name]
        environment = {
            "METRIC_NAMESPACE": "AgentBoundaries",
            "STATUTORY_MAXIMUM": "50000",
            "CONFIDENCE_THRESHOLD": "0.8",
            "AWARDS_TABLE": TABLE,
            "BEDROCK_MODEL_ID": MODEL_ID,
            "GUARDRAIL_ID": guardrail_id,
            "GUARDRAIL_VERSION": guardrail_version,
        }
        try:
            created = awslambda.create_function(
                FunctionName=name,
                Runtime=RUNTIME,
                Role=role_arn,
                Handler="lambda_function.lambda_handler",
                Code={"ZipFile": zip_source(source)},
                Timeout=timeout,
                MemorySize=256,
                Environment={"Variables": environment},
                Description="Boundary-first end-to-end sample",
            )
            arns[name] = created["FunctionArn"]
            print(f"  created function {name}")
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceConflictException":
                raise
            awslambda.update_function_code(FunctionName=name, ZipFile=zip_source(source))
            awslambda.get_waiter("function_updated_v2").wait(FunctionName=name)
            awslambda.update_function_configuration(
                FunctionName=name,
                Role=role_arn,
                Environment={"Variables": environment},
            )
            arns[name] = awslambda.get_function(FunctionName=name)["Configuration"][
                "FunctionArn"
            ]
            print(f"  updated function {name}")
        awslambda.get_waiter("function_active_v2").wait(FunctionName=name)
    return arns


def ensure_log_group():
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    # Execution data includes the resident narrative, so do not keep it forever.
    logs.put_retention_policy(
        logGroupName=LOG_GROUP, retentionInDays=LOG_RETENTION_DAYS
    )
    for group in logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP).get(
        "logGroups", []
    ):
        if group["logGroupName"] == LOG_GROUP:
            arn = group["arn"]
            return arn if arn.endswith(":*") else f"{arn}:*"
    raise RuntimeError("log group ARN not resolvable")


def create_state_machine(role_arn, function_arns, topic_arn, log_group_arn):
    definition = (HERE / "state_machine.asl.json").read_text()
    for placeholder, value in {
        "${ClassifyFunctionArn}": function_arns[f"{PREFIX}-classify"],
        "${ComputeAwardFunctionArn}": function_arns[f"{PREFIX}-compute-award"],
        "${ValidateOutputFunctionArn}": function_arns[f"{PREFIX}-validate-output"],
        "${RecordAwardFunctionArn}": function_arns[f"{PREFIX}-record-award"],
        "${HumanReviewTopicArn}": topic_arn,
    }.items():
        definition = definition.replace(placeholder, value)
    assert "${" not in definition, "unsubstituted placeholder remains"

    validation = sfn.validate_state_machine_definition(
        definition=definition, type="STANDARD"
    )
    if validation["result"] != "OK":
        for diagnostic in validation.get("diagnostics", []):
            print(f"    {diagnostic['severity']}: {diagnostic['message']}")
        raise SystemExit("ASL did not validate")
    print("  ASL validated OK")

    logging_config = {
        "level": "ALL",
        "includeExecutionData": True,
        "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": log_group_arn}}],
    }
    try:
        created = sfn.create_state_machine(
            name=STATE_MACHINE,
            definition=definition,
            roleArn=role_arn,
            type="STANDARD",
            loggingConfiguration=logging_config,
            tracingConfiguration={"enabled": True},
        )
        print(f"  created state machine {STATE_MACHINE}")
        return created["stateMachineArn"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "StateMachineAlreadyExists":
            raise
        arn = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{STATE_MACHINE}"
        sfn.update_state_machine(
            stateMachineArn=arn,
            definition=definition,
            roleArn=role_arn,
            loggingConfiguration=logging_config,
            tracingConfiguration={"enabled": True},
        )
        print(f"  updated state machine {STATE_MACHINE}")
        return arn


def create_alarm():
    cloudwatch.put_metric_alarm(
        AlarmName=ALARM,
        AlarmDescription="Elevated intermediate boundary violation rate",
        MetricName="BoundaryViolation",
        Namespace="AgentBoundaries",
        Statistic="Sum",
        Period=300,
        EvaluationPeriods=1,
        Threshold=3,
        ComparisonOperator="GreaterThanThreshold",
        Dimensions=[{"Name": "BoundaryType", "Value": "Intermediate"}],
        TreatMissingData="notBreaching",
    )
    print(f"  created alarm {ALARM}")


def main():
    print(f"Deploying {PREFIX} stack to {REGION}, account {ACCOUNT}")
    print(f"Model {MODEL_ID}\n")

    print("data and boundaries:")
    create_table()
    guardrail_id, guardrail_version = create_guardrail()
    review_key_arn = ensure_review_key()
    topic_arn, queue_url = create_messaging(review_key_arn)

    print("\nroles:")
    write_logs = {
        "Effect": "Allow",
        "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": [
            f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{PREFIX}-*",
            f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{PREFIX}-*:log-stream:*",
        ],
    }
    # PutMetricData supports no resource ARN, so the namespace condition is
    # the scope: these roles can publish only boundary metrics.
    put_metrics = {
        "Effect": "Allow",
        "Action": ["cloudwatch:PutMetricData"],
        "Resource": "*",
        "Condition": {"StringEquals": {"cloudwatch:namespace": "AgentBoundaries"}},
    }
    lambda_roles = {
        CLASSIFY_ROLE: ensure_role(
            CLASSIFY_ROLE,
            "lambda.amazonaws.com",
            {
                "Version": "2012-10-17",
                "Statement": [
                    write_logs,
                    put_metrics,
                    {
                        "Effect": "Allow",
                        "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
                        "Resource": model_resource_arns(MODEL_ID),
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["bedrock:ApplyGuardrail"],
                        "Resource": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/{guardrail_id}",
                    },
                ],
            },
        ),
        METRICS_ROLE: ensure_role(
            METRICS_ROLE,
            "lambda.amazonaws.com",
            {"Version": "2012-10-17", "Statement": [write_logs, put_metrics]},
        ),
        RECORD_ROLE: ensure_role(
            RECORD_ROLE,
            "lambda.amazonaws.com",
            {
                "Version": "2012-10-17",
                "Statement": [
                    write_logs,
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:PutItem"],
                        "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}",
                    },
                ],
            },
        ),
    }
    sfn_role = ensure_role(
        SFN_ROLE,
        "states.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunction",
                    "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{PREFIX}-*",
                },
                {"Effect": "Allow", "Action": "sns:Publish", "Resource": topic_arn},
                {
                    "Effect": "Allow",
                    "Action": ["kms:GenerateDataKey*", "kms:Decrypt"],
                    "Resource": review_key_arn,
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogDelivery",
                        "logs:GetLogDelivery",
                        "logs:UpdateLogDelivery",
                        "logs:DeleteLogDelivery",
                        "logs:ListLogDeliveries",
                        "logs:PutResourcePolicy",
                        "logs:DescribeResourcePolicies",
                        "logs:DescribeLogGroups",
                    ],
                    # Vended log delivery supports no resource-level scoping.
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                        "xray:GetSamplingRules",
                        "xray:GetSamplingTargets",
                    ],
                    # These X-Ray actions support no resource-level scoping.
                    "Resource": "*",
                },
            ],
        },
    )
    print("  waiting for IAM propagation")
    time.sleep(15)

    print("\nfunctions:")
    function_arns = create_functions(lambda_roles, guardrail_id, guardrail_version)

    print("\nworkflow:")
    log_group_arn = ensure_log_group()
    state_machine_arn = create_state_machine(
        sfn_role, function_arns, topic_arn, log_group_arn
    )

    print("\nobservability:")
    create_alarm()

    (HERE / "stack.json").write_text(
        json.dumps(
            {
                "region": REGION,
                "modelId": MODEL_ID,
                "stateMachineArn": state_machine_arn,
                "topicArn": topic_arn,
                "queueUrl": queue_url,
                "table": TABLE,
                "guardrailId": guardrail_id,
                "guardrailVersion": guardrail_version,
                "functions": function_arns,
            },
            indent=2,
        )
    )
    print(f"\nStack written to endtoend/stack.json")
    print("Next: python endtoend/run_cases.py")
    print("When finished: python endtoend/teardown.py")


if __name__ == "__main__":
    main()

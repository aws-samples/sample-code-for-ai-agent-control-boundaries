# Boundaries before agents: Designing and proving AI controls on AWS

Sample code accompanying the AWS blog post *Boundaries before agents: Designing and proving AI controls on AWS*.

The post describes five typed boundaries you place around AI agents so that routing is deterministic, outputs are validated, irreversible actions require human approval, and every decision produces an audit trail. This repository contains a runnable reference implementation of each boundary.

The repository has two parts. The per-boundary samples each demonstrate one boundary in isolation, so you can adapt it to your own architecture. The `endtoend/` stack deploys every boundary together and grades a set of cases against known correct answers. The samples create the AWS Identity and Access Management (IAM) roles they need, scoped to the resources they create.

## The five boundaries

| Boundary | Question it answers | Sample |
|:---|:---|:---|
| Input | Should this data reach the model at all? | [`input-output/guardrail.py`](input-output/guardrail.py) |
| Intermediate | Is this step's output good enough for the next step? | [`intermediate/validator.py`](intermediate/validator.py) |
| Output | Is this safe to show a user or act on? | [`input-output/guardrail.py`](input-output/guardrail.py) |
| Control | Who decides the next step, code or the model? | [`control-safety/state_machine.asl.json`](control-safety/state_machine.asl.json), [`gateway-authorization/setup_gateway.py`](gateway-authorization/setup_gateway.py) |
| Safety | Can this action be undone if it is wrong? | [`control-safety/state_machine.asl.json`](control-safety/state_machine.asl.json) |

Boundary health is measured in [`observability/boundary_metric.py`](observability/boundary_metric.py).

## Contents

```
common/
  model_lifecycle.py            Fail fast when a configured model has gone LEGACY
input-output/
  guardrail.py                  Amazon Bedrock Guardrails: create, then demonstrate
                                PII blocking, prompt-attack filtering, and grounding
control-safety/
  state_machine.asl.json        Step Functions definition with Choice-based routing,
                                Retry/Catch, and a waitForTaskToken human approval gate
  deploy_state_machine.py       Validate and optionally deploy the definition
  test_state_machine.py         Checks retry, escalation, and timeout contracts locally
intermediate/
  validator.py                  Lambda handler that enforces required fields,
                                a statutory ceiling, and a confidence threshold
  test_validator.py             Exercises all four validator branches locally
gateway-authorization/
  award_threshold.cedar         Cedar policy enforcing the award threshold
  tool_schema.json              Tool schema showing the required integer parameter type
  record_award_tool.py          Lambda handler behind the record_award tool
  setup_gateway.py              Create the gateway, policy, and Lambda tool target,
                                then prove the policy allows and denies live calls
  test_tool_schema.py           Checks the schema, policy, and target agree locally
observability/
  boundary_metric.py            Emit the BoundaryViolation metric and create an alarm
  test_boundary_metric.py       Checks the alarm watches a published dimension set
evaluation/
  measure_reliability.py        Measure whether the boundaries change outcomes,
                                graded against known ground truth per case
endtoend/
  deploy.py                     Deploy every boundary as one stack (bfa-e2e prefix)
  run_cases.py                  Run the ground-truth cases through the deployed stack
  teardown.py                   Delete the stack and confirm nothing remains
  lambdas/                      Classify, compute, validate, and record functions
run_tests.py                    Run every local test suite
cleanup.py                      Delete resources created by the per-boundary samples
requirements.txt                Python dependencies
```

## Measured results

`evaluation/measure_reliability.py` runs 8 benefits cases through a 5-step
pipeline under three conditions. Each case has a documented correct award or a
documented requirement to escalate. The script counts **silent failures**:
results that pass every structural check and are still wrong.

Silent failures in two runs per model, 24 per condition per run (8 cases, 3 runs each):

| Condition | Claude Sonnet 5, run 1 | Claude Sonnet 5, run 2 | Claude Haiku 4.5, run 1 | Claude Haiku 4.5, run 2 |
|:---|:---:|:---:|:---:|:---:|
| no validation between steps | 0 | 0 | 0 | 2 (8%) |
| schema boundary validating each step | 0 | 0 | **3 (12%)** | **4 (17%)** |
| award computed in code from extracted facts | 0 | 0 | 0 | 0 |

- **Schema validation** checks shape, type and range, so an award of `9600`
  where the rules give `7320` passes. Across all 96 runs per condition it let
  7 wrong awards through.
- **Computing the award in code** let none through on either model in either
  run. Extraction errors remain, so cases the model cannot extract cleanly
  escalate: Sonnet 5 escalated 16 of 24 runs and Haiku 9 of 24 in each run.
- At 24 runs, a difference between two conditions on one model is within noise.
  Haiku's no-validation count was 0 in one run and 2 in the other. `temperature`
  is not set, because Claude Sonnet 5 rejects it, so runs are not pinned to
  identical sampling.

The case set is small and hand-built. Extend it before drawing conclusions for
your own workload. `evaluation/results.json` holds only the most recent run
(Claude Haiku 4.5, run 2), because each run overwrites it.

```bash
.venv/bin/python evaluation/measure_reliability.py --runs 3
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  .venv/bin/python evaluation/measure_reliability.py --runs 3
```

## Prerequisites

- An AWS account with permissions for Amazon Bedrock, Amazon Bedrock AgentCore, AWS Step Functions, AWS Lambda, Amazon CloudWatch, and IAM role creation. The end-to-end stack also uses Amazon DynamoDB, Amazon Simple Notification Service (Amazon SNS), and Amazon Simple Queue Service (Amazon SQS).
- Python 3.11 or later.
- boto3 1.43.0 or later. Earlier versions, such as 1.42.0, do not include the AgentCore Policy APIs.
- Amazon Bedrock model access enabled in your AWS Region.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Set your Region if it differs from the default of `us-west-2`:

```bash
export AWS_REGION=us-west-2
```

## Regional availability

Amazon Bedrock AgentCore, Amazon Bedrock Guardrails, and Step Functions are available in select AWS Regions. Individual capabilities, such as AgentCore Policy, can have different availability than the core service. Confirm availability in your Region before running the samples, for example by checking that `aws bedrock-agentcore-control list-gateways` succeeds.

Model IDs change over time. `endtoend/deploy.py` and `evaluation/measure_reliability.py` check the model at startup with `common/model_lifecycle.py` and fail with ACTIVE alternatives if it has gone LEGACY. See "Model IDs and model responses" below.

## Running the samples

Each sample runs independently. Start with the tests, which create no resources:

```bash
.venv/bin/python run_tests.py                            # all local tests, no AWS calls
.venv/bin/python control-safety/deploy_state_machine.py  # validates by default
```

Then the samples that create real resources:

```bash
.venv/bin/python input-output/guardrail.py
.venv/bin/python observability/boundary_metric.py
.venv/bin/python gateway-authorization/setup_gateway.py
```

`guardrail.py` deletes its guardrail after the demonstration unless you pass `--keep`. `setup_gateway.py` ends by calling the `record_award` tool twice, with an amount under the ceiling and one over it, and exits non-zero unless the first is allowed and the second is denied.

To run every boundary together and grade the outcomes against known correct answers:

```bash
.venv/bin/python endtoend/deploy.py
.venv/bin/python endtoend/run_cases.py
.venv/bin/python endtoend/teardown.py
```

`run_cases.py` includes a simulated reviewer that answers the human-approval step, so the approval step runs end to end.

## Model IDs and model responses

- **Check the foundation model, not the inference profile.** A profile can
  report `ACTIVE` while its model is LEGACY or past end of life:

  ```
  us.anthropic.claude-sonnet-4-20250514-v1:0   profile ACTIVE, foundation model LEGACY
  us.anthropic.claude-3-haiku-20240307-v1:0    profile ACTIVE, foundation model past end of life
  ```

  `common/model_lifecycle.py` checks `modelLifecycle.status` on the foundation
  model, treats an ID missing from the catalogue as retired, and fails with a
  list of ACTIVE alternatives.
- **Claude Sonnet 5 rejects `temperature`.** Passing `temperature=0.0` raises
  `ValidationException: "temperature" is deprecated for this model`, so every
  sample omits it.
- **Do not index `content[0]["text"]`.** Claude Sonnet 5 can return a
  `reasoningContent` block first, which raises `KeyError: 'text'`. Scan the
  content blocks instead:

  ```python
  text = "".join(b["text"] for b in response["output"]["message"]["content"] if "text" in b)
  ```

Set `BEDROCK_MODEL_ID` to override the default model.

## Tests

```bash
.venv/bin/python run_tests.py
```

Eight suites, all offline: no AWS calls, no credentials, no deployed resources.

| Suite | Contract it enforces |
|:---|:---|
| [`intermediate/test_validator.py`](intermediate/test_validator.py) | Every validator branch, including a required field present but `null`, and a model reply that is prose or leads with a thinking block |
| [`control-safety/test_state_machine.py`](control-safety/test_state_machine.py) | Optimized integrations use service-prefixed error names, and plain Lambda tasks use bare ones<br>Every Lambda task that calls Bedrock retries `ThrottlingException`<br>No retrier mixes a wildcard with specific names<br>Every retried task and every Lambda task can escalate<br>Every award write is followed by a check that it was recorded<br>Every `waitForTaskToken` state has a timeout<br>No state is unreachable |
| [`observability/test_boundary_metric.py`](observability/test_boundary_metric.py) | The alarm watches a dimension set the code publishes, and `TreatMissingData` is explicit |
| [`gateway-authorization/test_tool_schema.py`](gateway-authorization/test_tool_schema.py) | Numerically compared parameters are declared `integer`<br>The Cedar ceiling matches the validator's `STATUTORY_MAXIMUM`<br>The Cedar action names the tool the Lambda target exposes<br>The schema uses only keys the gateway accepts<br>The committed policy keeps its ARN placeholder |
| [`endtoend/test_fact_cross_check.py`](endtoend/test_fact_cross_check.py) | Every number the model extracts appears in the case record<br>Correct extractions for each case pass |
| [`endtoend/test_compute_award.py`](endtoend/test_compute_award.py) | The award formula gives each evaluation case its expected total<br>A stated zero period, a missing one-time amount, and out-of-range facts escalate<br>The ceiling cites G-0.2<br>The evaluation imports this formula rather than keeping its own copy |
| [`endtoend/test_alarm_dimensions.py`](endtoend/test_alarm_dimensions.py) | The alarm `deploy.py` creates watches a dimension set the end-to-end Lambdas publish when their own code runs |
| [`endtoend/test_record_award.py`](endtoend/test_record_award.py) | The award write is conditional, a second write for the same case is refused without changing the first, and other errors raise |

## Implementation notes

**A tool-call policy needs a tool to act on.** Cedar policies at the gateway evaluate tool calls. A gateway whose only target is an inference target exposes no tools, so a policy naming `AwardToolTarget___record_award` never evaluates a call, while every setup step reports success. Expose the tool through an MCP target, here a Lambda function, and test the policy with a call it must allow and one it must deny.

**Numeric tool parameters must be declared `integer`.** A parameter typed `number` reaches Cedar as a string, and a comparison such as `context.input.amount <= 50000` then falls through to the default deny. See [`gateway-authorization/tool_schema.json`](gateway-authorization/tool_schema.json).

**A Cedar policy resource must name a specific gateway ARN.** A wildcard resource is rejected, and the policy cannot reference a gateway that does not exist yet, so create the gateway first.

**A null is not a missing key.** When you instruct a model not to invent values, it returns `null`, and a `key not in output` check passes. The value then reaches a comparison as `None` and raises. A validator that raises loses the reason, never records the boundary metric, and makes correctness depend on a `Catch` clause. See the rules at the top of [`intermediate/validator.py`](intermediate/validator.py).

## Clean up

These samples create billable resources. Delete them when you are finished:

```bash
.venv/bin/python cleanup.py              # per-boundary samples, bfa-sample prefix
.venv/bin/python endtoend/teardown.py    # end-to-end stack, bfa-e2e prefix
```

Both accept `--dry-run` to list what would be deleted. `teardown.py` ends with a sweep that waits for asynchronous deletions and reports anything still standing. Review the output and confirm nothing remains in the console.

## Security

This is sample code, for non-production usage. You should work with your security and
legal teams to meet your organizational security, regulatory, and compliance requirements
before deployment. Do not deploy this content to production environments without
additional security testing.

If you discover a potential security issue in this project, notify AWS Security through the
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
rather than opening a public issue.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

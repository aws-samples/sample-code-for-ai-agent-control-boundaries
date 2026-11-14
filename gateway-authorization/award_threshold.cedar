// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Control boundary at the gateway: authorize a tool call before it executes.
//
// Cedar evaluates four inputs on every call: a principal, an action, a
// resource, and request context. The action is the specific tool, the resource
// is the gateway, and the context carries the tool inputs.
//
// Two constraints that are easy to get wrong:
//
//   1. The resource must name a specific gateway ARN. A wildcard resource is
//      rejected, and the policy cannot reference a gateway that does not exist
//      yet, so create the gateway first.
//
//   2. The action id follows the {targetName}___{toolName} format, and the
//      target must exist first: a policy naming a tool no target exposes
//      fails creation with "unrecognized action".
//
// The amount range here matches intermediate/validator.py: no negative amount,
// and nothing above STATUTORY_MAXIMUM. Keeping both checks makes them defense
// in depth. Read the value from Parameter Store in production so the two
// cannot drift.
//
// The principal is the award agent's assumed role. For an IAM-authorized
// gateway the principal is AgentCore::IamEntity, and its id is the caller's
// ARN in the form arn:aws:sts::<account>:assumed-role/<role-name>. Any other
// identity that can reach the gateway is denied by default.
//
// setup_gateway.py substitutes GATEWAY_ARN_PLACEHOLDER and
// AGENT_PRINCIPAL_PLACEHOLDER with real values.

permit(
  principal == AgentCore::IamEntity::"AGENT_PRINCIPAL_PLACEHOLDER",
  action == AgentCore::Action::"AwardToolTarget___record_award",
  resource == AgentCore::Gateway::"GATEWAY_ARN_PLACEHOLDER"
)
when { context.input.amount >= 0 && context.input.amount <= 50000 };

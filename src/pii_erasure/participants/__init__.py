"""The eight real AWS services behind the five-verb contract.

Each participant is one Lambda function registered as an AgentCore Gateway target
(ARCHITECTURE §4). Inherit from `_base.Participant`; never re-implement the verb
plumbing — conformance is parameterised over the registry, so a copied harness is a
harness that drifts out from under the tests that cover it.
"""

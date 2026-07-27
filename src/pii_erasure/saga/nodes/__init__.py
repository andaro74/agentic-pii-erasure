"""Deterministic executor nodes (invariant 2).

Plain Python over state. No node constructs an agent or a model client, calls a model,
or branches on model output — replay of an approved plan never re-enters the model.
Enforced three ways: the AST test in `tests/unit/test_no_model_client.py`, the absence
of any `bedrock:*` action on the execution role (invariant 12, synth-asserted), and
review of this package.
"""

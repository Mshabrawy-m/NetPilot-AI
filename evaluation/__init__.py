"""Offline AI quality / groundedness evaluation framework (Phase B4).

The package is intentionally **not** part of the runtime application. It is a
developer/evaluation harness: it never calls a provider, never touches a real
network, never writes application configuration, and never executes code named
by the dataset. See ``evaluation/README.md`` for the documented command and
metric definitions.
"""

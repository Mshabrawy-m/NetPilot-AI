"""NetPilot API package.

``readmodels`` holds the pure, HTTP-free view functions (unit-testable with no
FastAPI installed); ``app`` is the optional FastAPI surface over them. Importing
this package does NOT import FastAPI - only ``api.app`` does - so the offline
test suite never gains a hard dependency on the web framework.
"""

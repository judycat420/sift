"""iNaturalist ingest: the only part of Sift permitted to talk to the API.

WHY THIS SUBPACKAGE EXISTS
--------------------------
Every network call Sift makes to iNaturalist goes through here, so that rate
limiting, caching, User-Agent identification and response-shape validation
happen in one place and cannot be bypassed by a caller in a hurry. Code outside
this subpackage receives parsed, validated Sift models and has no way to reach
the API.

INVARIANT PROTECTED
-------------------
Nothing here fabricates. Every field in a returned model came from a response
body; a response missing a field we need produces a drop with a reason, never a
default. Callers cannot tell the difference between a cached and a live result,
which is what makes the pipeline resumable.
"""

__all__: list[str] = []

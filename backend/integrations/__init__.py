"""Outbound adapters to services outside AWS.

Everything in this package is transport. An adapter receives a finished,
deterministic result from the engine and delivers it; none of them calculates
a business figure, and none is permitted to become a decision point.

Kept apart from `engine/` deliberately. The engine is pure and has no network,
no credentials and no AWS; these modules have all three, and the boundary is
easier to hold when it is also a directory.
"""

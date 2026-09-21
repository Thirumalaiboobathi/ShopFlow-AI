"""Operational telemetry. Not business logic, and deliberately separate from it.

Nothing in this package may import from `engine`, and nothing in `engine` may
import from here. Metrics describe how the system is running; the engines
describe what the shop is owed. Keeping them apart is what stops a rupee
figure from ever becoming a CloudWatch dimension.
"""

"""Autopilot / Nightshift: put idle inference to useful, verified work.

Two rules shape everything in this package:

1. Autonomous work must have positive expected value. If nothing worthwhile,
   safe and verifiable is found, autopilot stops.
2. Protect scarce long-term (weekly) quota before harvesting short-term idle
   capacity.

Stdlib-only, like the hook path: `budget` is imported by the in-session advisor.
"""

"""Pure functions turning raw Garmin payloads into normalized rows.

They never perform I/O and must tolerate missing fields: anything absent
becomes ``None``.
"""

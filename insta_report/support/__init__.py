"""Cross-cutting services: filesystem locations and secret redaction.

Deliberately not filed under ``browser/`` or ``api/``: the artifact store and
the checkpoint writer are used by every channel, and the runner reaches them
directly.
"""

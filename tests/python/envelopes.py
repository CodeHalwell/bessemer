"""Decode worker response envelopes for direct Worker tests."""

import json


def payload(result):
    """Return ``(status, value)`` for natively encoded or general JSON envelopes.

    Results made of exact built-in JSON values are encoded by the worker before
    they reach native code; other results keep the ``(status, value)`` form.
    """
    if (
        isinstance(result, tuple)
        and len(result) == 4
        and result[3] == "json_bytes"
        and not result[2]
    ):
        return result[0], json.loads(result[1])
    return result

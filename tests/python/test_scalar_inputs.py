"""Typed scalar inputs convert to the same Python objects and responses.

``scalar_inputs_golden.json`` holds the status and exact body bytes each request
below got before path, query and form scalars stopped going through JSON
numbers: ints at and beyond the i64 range, floats, booleans, percent-encoded
strings, constraint and parsing errors. Both event loops must still match it.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

from bessemer import App, Query

NATIVE = importlib.util.find_spec("bessemer._native") is not None
GOLDEN = json.loads(
    (
        Path(__file__).parent.parent / "fixtures" / "scalar_inputs_golden.json"
    ).read_text()
)


def build():
    app = App()

    @app.get("/i/{n}")
    async def i(n: int):
        return {"v": n, "t": type(n).__name__}

    @app.get("/f/{x}")
    async def f(x: float):
        return {"v": x, "t": type(x).__name__}

    @app.get("/s/{v}")
    async def s(v: str):
        return {"v": v}

    @app.get("/b")
    async def b(
        flag: bool = False,
        n: int = Query(1, ge=-5, lt=10),
        r: float = Query(0.5, gt=0, le=1.5),
    ):
        return {
            "flag": flag,
            "n": n,
            "r": r,
            "t": [type(flag).__name__, type(n).__name__, type(r).__name__],
        }

    @app.get("/files/{rest:path}")
    async def files(rest: str):
        return {"rest": rest}

    return app


CASES = (
    [
        f"/i/{v}"
        for v in [
            "7",
            "0",
            "-0",
            "007",
            "+7",
            "-5",
            "9223372036854775807",
            "9223372036854775808",
            "-9223372036854775808",
            "-9223372036854775809",
            "18446744073709551615",
            "18446744073709551616",
            "1" + "0" * 40,
            "abc",
            "1_0",
            "1.5",
            "",
            "%37",
        ]
    ]
    + [
        f"/f/{v}"
        for v in ["1.5", "-0.0", "1e3", "7", "nan", "inf", "abc", "1e400", "0.1"]
    ]
    + [
        f"/s/{v}"
        for v in ["plain", "a%20b%2Fc", "%E2%82%AC", "%ff", "caf%C3%A9", "a+b"]
    ]
    + ["/files/", "/files/a%20b/c", "/files/%ff"]
    + [
        f"/b?flag={v}"
        for v in [
            "true",
            "TRUE",
            "Yes",
            "on",
            "1",
            "false",
            "Off",
            "0",
            "no",
            "maybe",
            "",
        ]
    ]
    + [
        f"/b?n={v}"
        for v in [
            "-5",
            "-6",
            "9",
            "10",
            "9223372036854775807",
            "9223372036854775808",
            "-9223372036854775809",
            "x",
            "007",
        ]
    ]
    + [f"/b?r={v}" for v in ["1.5", "1.6", "0", "1e0", "nan", "0.25"]]
)


class ScalarInputGoldenTests(unittest.TestCase):
    event_loop = "asyncio"

    def test_the_recording_covers_every_request(self):
        self.assertEqual(sorted(CASES), sorted(GOLDEN))

    def test_responses_match_the_recorded_bytes(self):
        from bessemer.testclient import TestClient

        with TestClient(
            build(), server_options={"event_loop": self.event_loop}
        ) as client:
            for path, (status, content) in GOLDEN.items():
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(
                        [response.status_code, response.content.decode()],
                        [status, content],
                    )


@unittest.skipUnless(
    NATIVE and sys.platform in ("linux", "darwin"), "the Rust loop needs Linux or macOS"
)
class ScalarInputGoldenRustLoopTests(ScalarInputGoldenTests):
    event_loop = "bessemer"


if __name__ == "__main__":
    unittest.main()

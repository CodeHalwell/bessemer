#!/usr/bin/env python3
"""Seeded, bounded cross-language property checks against isolated native workers.

Run with the installed wheel and hypothesis==6.170.14, for example:
  python tests/security/fuzz_transport.py --examples 2000 --output target/fuzz

Each selected property gets --examples generated cases. The default seven
properties therefore request 14000 cases. This is semantic/protocol testing,
not a coverage-guided native fuzzer or a performance qualification.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path

from hypothesis import HealthCheck, Phase, given, seed, settings
from hypothesis import strategies as st

APPLICATION = r"""
from bessemer import App, Cookie, Header, Request, Response
app = App(openapi_url=None, allowed_hosts=[
    "127.0.0.1", "localhost", "good.test", "*.example.test", "::1"
])
app.health("/healthz")

@app.post("/echo")
async def echo(item: dict):
    return item

@app.post("/raw-json")
async def raw_json(request: Request):
    return await request.json()

@app.get("/cookie")
async def cookie(request: Request, session: str = Cookie()):
    return {"bound": session, "request": request.cookies["session"]}

@app.get("/header")
async def header(request: Request, value: str = Header(alias="X-Value")):
    return {"bound": value, "request": request.headers["x-value"]}

class NativeEnvelope(Response):
    def __init__(self, name, value):
        super().__init__()
        self.name, self.value = name, value

    def to_native(self, *, max_bytes=None):
        return 200, b"ok", [(self.name, self.value)], "bytes"

@app.post("/response-header")
async def response_header(item: dict):
    return NativeEnvelope(item["name"], item["value"])
"""

FORBIDDEN_HEADERS = {
    "connection",
    "content-length",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}
TOKEN_CHARACTERS = (
    "!#$%&'*+-.^_|~"
    + chr(96)
    + "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
TOKEN = re.compile("[" + re.escape(TOKEN_CHARACTERS) + "]+\\Z", re.ASCII)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def package_provenance(package, wheel):
    root = Path(package.__file__).parent
    files = {
        str(path.relative_to(root)): digest(path)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".so"}
    }
    if wheel is not None:
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.startswith("bessemer/") and not name.endswith("/"):
                    relative = name.removeprefix("bessemer/")
                    expected = hashlib.sha256(archive.read(name)).hexdigest()
                    if files.get(relative) != expected:
                        raise ValueError(
                            f"--wheel does not match installed package file: {relative}"
                        )
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def serializable(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, tuple):
        return [serializable(item) for item in value]
    if isinstance(value, list):
        return [serializable(item) for item in value]
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    return value


def equivalent(actual, expected):
    if type(actual) is not type(expected):
        raise AssertionError(
            f"JSON type changed: {type(expected).__name__} -> {type(actual).__name__}"
        )
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), "JSON object keys changed"
        for key in expected:
            equivalent(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected), "JSON array length changed"
        for received, original in zip(actual, expected, strict=True):
            equivalent(received, original)
    elif isinstance(expected, float):
        assert actual == expected, "floating-point value changed"
        if expected == 0:
            assert math.copysign(1, actual) == math.copysign(1, expected), (
                "negative zero changed"
            )
    else:
        assert actual == expected, "JSON scalar value changed"


class NativeServer:
    def __init__(self, response_limit, directory, output):
        self.response_limit = response_limit
        self.directory = directory
        self.output = output
        directory.mkdir()
        (directory / "fixture.py").write_text(APPLICATION)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.log = (directory / "server.log").open("w+")
        self.process = None
        self.connection = None
        try:
            self._start()
        except BaseException:
            self.close()
            raise

    def _start(self):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bessemer",
                "run",
                "fixture:app",
                "--port",
                str(self.port),
                "--workers",
                "1",
                "--max-body-size",
                "32768",
                "--max-response-size",
                str(self.response_limit),
                "--max-in-flight",
                "1",
                "--max-connections",
                "16",
                "--header-timeout",
                "1",
                "--max-header-size",
                "8192",
                "--max-request-target-size",
                "1024",
                "--request-timeout",
                "2",
                "--graceful-timeout",
                "1",
            ],
            cwd=self.directory,
            stdout=self.log,
            stderr=self.log,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("BESSEMER_")
            },
        )
        self.connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                self.connection.request("GET", "/healthz")
                response = self.connection.getresponse()
                response.read()
                if response.status == 200:
                    self.connection.close()
                    return
            except OSError:
                self.connection.close()
            time.sleep(0.02)
        self.log.seek(0)
        failure = self.log.read()
        raise AssertionError(f"isolated server failed to start: {failure}")

    def close(self):
        if self.connection is not None:
            self.connection.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.log.closed:
            return
        self.log.flush()
        shutil.copyfile(
            self.directory / "server.log",
            self.output / f"{self.directory.name}-server.log",
        )
        self.log.close()

    def post(self, path, value, *, whitespace=False):
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if whitespace:
            encoded = b" \r\n\t" + encoded + b"\r\n "
        return self.post_bytes(path, encoded)

    def post_bytes(self, path, encoded):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(
                "POST", path, encoded, {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def raw(self, path="/healthz", *, host="localhost", headers=(), target=None):
        target = path if target is None else target
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in headers)
            + "\r\n"
        ).encode("ascii")
        with socket.create_connection(
            ("127.0.0.1", self.port), timeout=3
        ) as connection:
            connection.sendall(request)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()


def bounded_tree(atoms, keys, *, max_nodes, max_depth, max_width):
    @st.composite
    def tree(draw):
        def value(depth, budget):
            kind = draw(st.integers(0, 2))
            if budget <= 1 or depth >= max_depth or kind == 0:
                return draw(atoms)
            count = draw(st.integers(0, min(max_width, budget - 1)))
            names = (
                draw(st.lists(keys, min_size=count, max_size=count, unique=True))
                if kind == 2
                else None
            )
            remaining = budget - 1
            values = []
            for index in range(count):
                allowance = draw(st.integers(1, remaining - (count - index - 1)))
                values.append(value(depth + 1, allowance))
                remaining -= allowance
            return (
                dict(zip(names, values, strict=True)) if names is not None else values
            )

        return value(0, max_nodes)

    return tree()


def strategies():
    text = st.text(st.characters(exclude_categories=("Cs",)), max_size=32)
    numbers = st.one_of(
        st.integers(-(2**512), 2**512),
        st.floats(allow_nan=False, allow_infinity=False),
    )
    atoms = st.one_of(st.none(), st.booleans(), numbers, text)
    keys = st.one_of(text, st.just("$serde_json::private::Number"))
    # At most63keys/64values, strings of at most192encoded bytes, and
    # integers of at most156bytes keep even the worst case below24KiB.
    tree = bounded_tree(atoms, keys, max_nodes=64, max_depth=5, max_width=4)
    budget_atoms = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-(2**100), 2**100),
        st.floats(allow_nan=False, allow_infinity=False),
        text,
    )
    budget_tree = bounded_tree(
        budget_atoms, keys, max_nodes=24, max_depth=4, max_width=3
    )
    label = st.text("abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)

    @st.composite
    def hosts(draw):
        left = draw(st.lists(label, min_size=1, max_size=4)).copy()
        port = draw(st.integers(0, 65535))
        allowed = ".".join([*left, "example", "test"])
        mode = draw(st.integers(0, 8))
        if mode == 0:
            return allowed, 200
        if mode == 1:
            return allowed.upper() + f".:{port}", 200
        if mode == 2:
            return draw(
                st.sampled_from(["good.test", "[::1]", "[0:0:0:0:0:0:0:1]"])
            ) + f":{port}", 200
        if mode == 3:
            return allowed + ".evil.test", 400
        if mode == 4:
            return ".".join(left) + "example.test", 400
        if mode == 5:
            return allowed + ":" + draw(
                st.sampled_from(["", "abc", "-1", "65536", "999999999999999999999"])
            ), 400
        if mode == 6:
            return "_" + allowed, 400
        if mode == 7:
            return "example.test", 400
        return allowed + "@evil.test", 400

    cookie_alphabet = "".join(
        chr(value) for value in range(33, 127) if value not in (34, 44, 59, 92)
    )
    cookies = st.tuples(
        st.text(cookie_alphabet, max_size=32), st.booleans(), st.booleans()
    )
    header_values = st.text(
        "".join(chr(value) for value in range(32, 127)) + "\t", max_size=40
    )
    request_headers = st.tuples(header_values, st.booleans())
    names = st.one_of(
        st.text(TOKEN_CHARACTERS, min_size=1, max_size=24),
        st.just("X-Forged"),
        st.sampled_from(sorted(FORBIDDEN_HEADERS)),
        st.sampled_from(["", "bad name", "bad:name", "bad\r\nname", "bad\x00name"]),
    )
    response_values = st.one_of(
        header_values,
        st.text(
            st.sampled_from(
                [chr(value) for value in range(32) if value != 9] + [chr(127)]
            ),
            min_size=1,
            max_size=5,
        ),
        st.just("value\r\nX-Forged: true"),
    )

    @st.composite
    def invalid_json(draw):
        kind = draw(
            st.sampled_from(
                ["surrogate", "utf8", "integer", "overflow", "depth", "syntax"]
            )
        )
        if kind == "surrogate":
            escaped = f"\\u{draw(st.integers(0xD800, 0xDFFF)):04x}".encode()
            context = draw(st.integers(0, 4))
            if context == 0:
                return b'{"' + escaped + b'":1}'
            if context == 1:
                value = b'["' + escaped + b'"]'
            elif context == 2:
                value = b'{"nested":"' + escaped + b'"}'
            else:
                value = b'"' + escaped + b'"'
        elif kind == "utf8":
            # One high byte followed by a quote cannot form valid UTF-8.
            value = b'"prefix' + bytes([draw(st.integers(128, 255))]) + b'"'
        elif kind == "integer":
            value = b"9" * draw(st.integers(4097, 4200))
            if draw(st.booleans()):
                value = b"-" + value
        elif kind == "overflow":
            value = (
                str(draw(st.integers(1, 9))).encode()
                + b"e+"
                + str(draw(st.integers(309, 9999))).encode()
            )
            if draw(st.booleans()):
                value = b"-" + value
        elif kind == "depth":
            depth = draw(st.integers(65, 75))
            value = b"[" * depth + b"0" + b"]" * depth
        else:
            return draw(
                st.sampled_from(
                    [
                        b"",
                        b"{",
                        b'{"x":1,}',
                        b'{"x":01}',
                        b'{"x":+1}',
                        b'{"x":NaN}',
                        b'{"x":Infinity}',
                        b'{"x":true "y":false}',
                        b'{"x":"raw\x00control"}',
                        b'{"x":/*comment*/1}',
                    ]
                )
            )
        suffix = b',"value":1}' if draw(st.booleans()) else b"}"
        return b'{"value":' + value + suffix

    return {
        "json": st.tuples(tree, st.booleans()),
        "budgets": budget_tree,
        "hosts": hosts(),
        "cookies": cookies,
        "request_headers": request_headers,
        "response_headers": st.tuples(names, response_values),
        "invalid_json": invalid_json(),
    }


def campaign(arguments):
    import bessemer
    import bessemer._native as native

    output = arguments.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "seed": arguments.seed,
        "examples_per_property": arguments.examples,
        "hypothesis_version": importlib.metadata.version("hypothesis"),
        "bessemer_version": bessemer.__version__,
        "python_version": platform.python_version(),
        "native_extension_sha256": digest(native.__file__),
        "installed_package_tree_sha256": package_provenance(bessemer, arguments.wheel),
        "script_sha256": digest(__file__),
        "wheel_sha256": digest(arguments.wheel) if arguments.wheel else None,
        "properties": {},
        "scope": "Isolated single-worker HTTP/1 services; bounded semantic properties, no coverage or performance acceptance claim.",
    }
    properties = strategies()
    selected = arguments.group or list(properties)
    started = time.monotonic()
    try:
        with (
            tempfile.TemporaryDirectory(prefix="bessemer-properties-") as directory,
            ExitStack() as stack,
        ):
            root = Path(directory)
            reference = NativeServer(32768, root / "reference", output)
            stack.callback(reference.close)
            bounded = NativeServer(256, root / "bounded", output)
            stack.callback(bounded.close)

            def json_check(case):
                expected = {"payload": case[0]}
                status, _, body = reference.post("/echo", expected, whitespace=case[1])
                assert status == 200, f"roundtrip returned HTTP{status}: {body[:300]!r}"
                equivalent(json.loads(body), expected)
                status, _, body = reference.post(
                    "/raw-json", case[0], whitespace=case[1]
                )
                assert status == 200, (
                    f"raw JSON roundtrip returned HTTP{status}: {body[:300]!r}"
                )
                equivalent(json.loads(body), case[0])

            def budget_check(value):
                expected = {"payload": value, "padding": ""}
                status, _, body = reference.post("/echo", expected)
                assert status == 200, f"reference returned HTTP{status}"
                if len(body) > 256:
                    assert bounded.post("/echo", expected)[0] == 500, (
                        "oversized response accepted"
                    )
                    return
                expected["padding"] = "x" * (256 - len(body))
                status, _, body = reference.post("/echo", expected)
                assert status == 200 and len(body) == 256, (
                    "reference padding oracle failed"
                )
                status, _, body = bounded.post("/echo", expected)
                assert status == 200, "exact256-byte response was rejected"
                assert len(body) == 256
                equivalent(json.loads(body), expected)
                expected["padding"] += "x"
                assert bounded.post("/echo", expected)[0] == 500, (
                    "257-byte response accepted"
                )

            def host_check(case):
                host, expected = case
                assert reference.raw(host=host)[0] == expected, (
                    f"Host classification changed: {host!r}"
                )

            def cookie_check(case):
                value, quoted, duplicate = case
                cookie = f'session="{value}"' if quoted else "session=" + value
                if duplicate:
                    cookie += "; session=duplicate"
                status, _, body = reference.raw("/cookie", headers=[("Cookie", cookie)])
                if duplicate:
                    assert status == 400, "duplicate cookie accepted"
                else:
                    assert status == 200, f"valid cookie returned HTTP{status}"
                    equivalent(json.loads(body), {"bound": value, "request": value})

            def request_header_check(case):
                value, duplicate = case
                headers = [("X-Value", value)] * (2 if duplicate else 1)
                status, _, body = reference.raw("/header", headers=headers)
                if duplicate:
                    assert status == 422, "duplicate scalar header accepted"
                else:
                    assert status == 200, f"valid scalar header returned HTTP{status}"
                    equivalent(
                        json.loads(body),
                        {"bound": value.strip(" \t"), "request": value.strip(" \t")},
                    )

            def response_header_check(case):
                name, value = case
                valid = (
                    bool(TOKEN.fullmatch(name))
                    and name.lower() not in FORBIDDEN_HEADERS
                    and all(
                        32 <= ord(character) <= 126 or character == "\t"
                        for character in value
                    )
                )
                status, headers, body = reference.post(
                    "/response-header", {"name": name, "value": value}
                )
                assert status == (200 if valid else 500), (
                    f"native response header classification changed: {case!r}"
                )
                if not valid or name.lower() != "x-forged":
                    assert "x-forged" not in {key.lower() for key in headers}, (
                        "response header injection"
                    )
                if valid:
                    assert body == b"ok"

            def invalid_json_check(case):
                for path in ("/echo", "/raw-json"):
                    status, _, body = reference.post_bytes(path, case)
                    assert status == 422, (
                        f"invalid JSON returned HTTP{status} from {path}"
                    )
                    assert b"Traceback" not in body
                assert reference.post("/echo", {"recovered": True})[0] == 200, (
                    "invalid JSON leaked Python admission capacity"
                )

            checks = {
                "json": json_check,
                "budgets": budget_check,
                "hosts": host_check,
                "cookies": cookie_check,
                "request_headers": request_header_check,
                "response_headers": response_header_check,
                "invalid_json": invalid_json_check,
            }

            def exercise(name, strategy, check, property_seed):
                stats = {
                    "attempts": 0,
                    "unique_cases": 0,
                    "passed": False,
                    "seed": property_seed,
                }
                report["properties"][name] = stats
                seen = set()

                @seed(property_seed)
                @settings(
                    max_examples=arguments.examples,
                    database=None,
                    deadline=None,
                    phases=[Phase.generate, Phase.shrink],
                    suppress_health_check=[HealthCheck.too_slow],
                )
                @given(strategy)
                def property_check(case):
                    fixture = serializable(case)
                    encoded = json.dumps(
                        fixture, ensure_ascii=True, sort_keys=True, allow_nan=False
                    )
                    seen.add(hashlib.sha256(encoded.encode()).hexdigest())
                    stats["attempts"] += 1
                    stats["unique_cases"] = len(seen)
                    try:
                        check(case)
                    except Exception as error:
                        (output / f"{name}-failure.json").write_text(
                            json.dumps(
                                {
                                    "property": name,
                                    "case": fixture,
                                    "error": str(error),
                                },
                                ensure_ascii=True,
                                indent=2,
                            )
                            + "\n"
                        )
                        raise

                try:
                    property_check()
                    stats["passed"] = True
                finally:
                    (output / f"{name}-digests.txt").write_text(
                        "\n".join(sorted(seen)) + "\n"
                    )
                print(json.dumps({"property": name, **stats}), flush=True)

            for offset, name in enumerate(selected):
                exercise(name, properties[name], checks[name], arguments.seed + offset)
            report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["error"] = repr(error)
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        )
    return report


def main():
    if not __debug__:
        raise RuntimeError(
            "property checks require Python assertions; remove -O/PYTHONOPTIMIZE"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=int, default=2000)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0xB355E2026)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--group", action="append", choices=list(strategies()))
    arguments = parser.parse_args()
    if not 1 <= arguments.examples <= 100_000:
        parser.error("--examples must be between1 and100000 per property")
    campaign(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

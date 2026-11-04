"""Public native-control configuration is strict before opening a listener."""

import json
import unittest
from dataclasses import FrozenInstanceError

from bessemer.middleware import CORS, validate_settings


class NativeConfigurationTests(unittest.TestCase):
    def test_defaults_are_safe_and_metrics_are_disabled(self):
        config = validate_settings({})
        self.assertTrue(config["request_id"])
        self.assertTrue(config["security_headers"])
        self.assertFalse(config["access_log"])
        self.assertIsNone(config["cors"])
        self.assertIsNone(config["api_key"])
        self.assertIsNone(config["metrics_path"])
        self.assertEqual(config["trusted_proxies"], [])
        self.assertIsNone(config["allowed_hosts"])

    def test_cors_copies_and_freezes_caller_sequences(self):
        origins = ["https://app.example.test"]
        policy = CORS(
            origins=origins, allow_methods=["GET", "POST"], allow_headers=["X-API-Key"]
        )
        origins.append("https://unexpected.example.test")
        self.assertEqual(policy.to_dict()["origins"], ["https://app.example.test"])
        with self.assertRaises(FrozenInstanceError):
            policy.max_age = 10
        exported = validate_settings({"cors": policy})
        exported["cors"]["origins"].append("https://another.example.test")
        self.assertEqual(policy.origins, ("https://app.example.test",))

    def test_credentials_require_explicit_origins(self):
        for config in (
            {"origins": ["*"], "allow_credentials": True},
            {"origins": ["*", "https://good.test"]},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_settings({"cors": config})
        self.assertTrue(
            validate_settings(
                {"cors": {"origins": ["https://good.test"], "allow_credentials": True}}
            )["cors"]["allow_credentials"]
        )

    def test_cors_rejects_non_origin_urls_and_header_injection(self):
        invalid_origins = [
            "https://good.test/",
            "https://good.test/path",
            "https://user@good.test",
            "https://good.test?query=x",
            "https://good.test?",
            "https://good.test#fragment",
            "https://good.test#",
            "https://good.test:",
            "https://good.test:99999",
            "file://good.test",
            "https://good.test\r\nInjected: x",
        ]
        for origin in invalid_origins:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CORS(origins=[origin])
        for headers in (
            ["X-Good", "Bad Header"],
            ["X-Good\r\nInjected"],
            ["*", "Authorization"],
        ):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                CORS(origins=["https://good.test"], allow_headers=headers)

    def test_cors_requires_correct_types_and_supported_methods(self):
        invalid = [
            {"origins": "https://good.test"},
            {"origins": None},
            {"allow_methods": ["get"]},
            {"allow_methods": []},
            {"allow_credentials": 1},
            {"max_age": True},
            {"max_age": -1},
            {"max_age": 86401},
            {"unexpected": True},
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_settings({"cors": config})

    def test_proxy_configuration_is_canonical_and_strict(self):
        self.assertEqual(
            validate_settings({"trusted_proxies": ["10.1.2.3/8", "127.0.0.1"]})[
                "trusted_proxies"
            ],
            ["10.0.0.0/8", "127.0.0.1/32"],
        )
        for proxies in (False, 0, "", {}, "127.0.0.1", ["invalid"], [None]):
            with self.subTest(proxies=proxies), self.assertRaises(ValueError):
                validate_settings({"trusted_proxies": proxies})

    def test_allowed_hosts_are_canonical_and_require_explicit_dns_or_ip_patterns(self):
        self.assertEqual(
            validate_settings(
                {
                    "allowed_hosts": [
                        "EXAMPLE.Test.",
                        "*.Example.Test",
                        "127.0.0.1",
                        "[2001:db8::1]",
                    ]
                }
            )["allowed_hosts"],
            ["example.test", "*.example.test", "127.0.0.1", "2001:db8::1"],
        )
        self.assertEqual(validate_settings({"allowed_hosts": []})["allowed_hosts"], [])
        for value in (
            "example.test",
            False,
            [None],
            ["*"],
            ["example.test:8000"],
            ["https://example.test"],
            ["foo..test"],
            ["-foo.test"],
            ["foo-.test"],
            ["*.127.0.0.1"],
            ["foo.*.test"],
            ["under_score.test"],
            ["[127.0.0.1]"],
            ["fe80::1%eth0"],
            ["x" * 64 + ".test"],
            ["example.test\r\nInjected"],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings({"allowed_hosts": value})

    def test_api_key_configuration_exports_only_environment_names(self):
        original = {"env": "SERVICE_API_KEY", "exclude_paths": ["/healthz"]}
        exported = validate_settings({"api_key": original})
        original["exclude_paths"].append("/private")
        self.assertEqual(
            exported["api_key"],
            {
                "env": "SERVICE_API_KEY",
                "header": "X-API-Key",
                "exclude_paths": ["/healthz"],
            },
        )
        self.assertNotIn("value", json.dumps(exported))
        for key in (
            {"value": "do-not-accept-plaintext"},
            {"env": "BAD ENV"},
            {"env": "KEY", "header": "Host"},
            {"env": "KEY", "header": "X-Request-ID"},
            {"env": "KEY", "header": "X-Forwarded-For"},
            {"env": "KEY", "header": "Origin"},
            {"env": "KEY", "header": "Access-Control-Request-Headers"},
            {"env": "KEY", "header": "X-Key\r\nInjected"},
            {"env": "KEY", "exclude_paths": ["/public/*"]},
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_settings({"api_key": key})

    def test_metrics_require_authentication_and_an_exact_path(self):
        for config in (
            {"metrics_path": "/metrics"},
            {"metrics_path": "/metrics?token=secret", "metrics_token_env": "TOKEN"},
            {"metrics_token_env": "TOKEN"},
            {"metrics_path": "/metrics", "metrics_token_env": "BAD TOKEN"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_settings(config)
        self.assertEqual(
            validate_settings(
                {"metrics_path": "/metrics", "metrics_token_env": "METRICS_TOKEN"}
            )["metrics_token_env"],
            "METRICS_TOKEN",
        )
        self.assertEqual(
            validate_settings(
                {"metrics_path": "/metrics", "api_key": {"env": "API_KEY"}}
            )["metrics_path"],
            "/metrics",
        )

    def test_rate_limit_memory_and_time_are_bounded(self):
        valid = validate_settings(
            {
                "rate_limit": {
                    "requests": 60,
                    "window_seconds": 60,
                    "exclude_paths": ["/healthz"],
                }
            }
        )["rate_limit"]
        self.assertEqual(valid["max_keys"], 10000)
        self.assertEqual(valid["window_seconds"], 60.0)
        for config in (
            {"requests": True, "window_seconds": 1},
            {"requests": 0, "window_seconds": 1},
            {"requests": 1, "window_seconds": float("nan")},
            {"requests": 1, "window_seconds": 0},
            {"requests": 1, "window_seconds": 86401},
            {"requests": 10000, "window_seconds": 1, "max_keys": 10000},
            {"requests": 1, "window_seconds": 1, "max_keys": 0},
            {"requests": 1, "window_seconds": 1, "unknown": True},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_settings({"rate_limit": config})

    def test_settings_do_not_coerce_boolean_flags_or_ignore_unknown_keys(self):
        for name in ("request_id", "security_headers", "access_log"):
            for value in (0, 1, None, "false"):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    validate_settings({name: value})
        with self.assertRaises(ValueError):
            validate_settings({"unrecognised": "must fail"})

    def test_exemptions_and_metrics_paths_reject_ambiguous_paths(self):
        for path in (
            "/café",
            "relative",
            "//host/path",
            "/query?x=1",
            "/fragment#x",
            "/item/{id}",
            "/item/*",
            "/back\\slash",
            "/space here",
            "/line\nbreak",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_settings(
                    {"api_key": {"env": "TOKEN", "exclude_paths": [path]}}
                )


if __name__ == "__main__":
    unittest.main()

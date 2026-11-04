from __future__ import annotations

# Declarative dependency defaults are part of the public API under test.
# ruff: noqa: B008
import inspect
import sys
import unittest
from datetime import date
from enum import Enum
from functools import partial
from typing import Annotated, Any, Literal
from unittest.mock import patch
from uuid import UUID

from bessemer.params import Body, Cookie, Depends, Header, Path, Query
from bessemer.request import BackgroundTasks, Request, State
from bessemer.schema import compile_inputs, response_adapter


class Colour(str, Enum):
    red = "red"
    blue = "blue"


class Search:
    def __init__(self, limit: Annotated[int, Query(ge=1)] = 10):
        self.limit = limit


class CallableSearch:
    async def __call__(self, query: str) -> dict:
        return {"query": query}


class SchemaTests(unittest.TestCase):
    def test_dependency_dag_shares_inputs_and_preserves_uncached_bindings(self):
        def token(authorization: Annotated[str, Header()]):
            return authorization

        def account(value=Depends(token)):
            return value

        async def handler(
            first=Depends(account), second=Depends(token, use_cache=False)
        ):
            return first, second

        plan = compile_inputs(handler, "/")
        self.assertEqual([node.call for node in plan.dependencies], [token, account])
        self.assertEqual(plan.dependencies[1].bindings[0].dependency, 0)
        self.assertEqual(plan.plan.bindings[1].dependency, 0)
        self.assertFalse(plan.plan.bindings[1].use_cache)
        self.assertEqual(len(plan.parameters), 1)
        self.assertEqual(plan.parameters[0]["alias"], "authorization")
        self.assertEqual(plan.parameters[0]["source"], "header")

    def test_class_dependency_uses_constructor_hints(self):
        def handler(search: Annotated[Search, Depends()]):
            return search.limit

        compiled = compile_inputs(handler, "/")
        self.assertIs(compiled.dependencies[0].call, Search)
        self.assertEqual(compiled.parameters[0]["constraints"], {"ge": 1})

    def test_callable_instances_and_partial_async_functions_keep_async_kind(self):
        instance = compile_inputs(CallableSearch(), "/")
        self.assertEqual(instance.plan.kind, "async")
        self.assertEqual(instance.parameters[0]["type"], "str")

        async def handler(query: str, *, limit: int = 10):
            return query

        compiled = compile_inputs(partial(handler, limit=5), "/")
        self.assertEqual(compiled.plan.kind, "async")
        self.assertEqual(compiled.parameters[1]["default"], 5)

    def test_patterns_use_non_backtracking_validation(self):
        def handler(value: Annotated[str, Query(pattern="^[a-z]+$")]):
            return value

        try:
            compiled = compile_inputs(handler, "/")
        except ValueError as error:
            if "optional Pydantic" in str(error):
                self.skipTest("optional Pydantic v2 is not installed")
            raise
        self.assertEqual(compiled.input_validators["value"]("valid"), "valid")
        with self.assertRaises(ValueError):
            compiled.input_validators["value"]("INVALID")

        def invalid(value: Annotated[str, Query(pattern="(?=a)a")]):
            return value

        with self.assertRaisesRegex(ValueError, "unsupported"):
            compile_inputs(invalid, "/")

    def test_cycles_fail_before_requests(self):
        def first(value):
            return value

        def second(value=Depends(first)):
            return value

        first.__defaults__ = (Depends(second),)

        def handler(value=Depends(first)):
            return value

        with self.assertRaisesRegex(ValueError, "cycle"):
            compile_inputs(handler, "/")

    def test_application_dependencies_can_use_state_only(self):
        def application(state: State):
            return state

        def handler(value=Depends(application, scope="app")):
            return value

        self.assertEqual(compile_inputs(handler, "/").dependencies[0].scope, "app")
        for annotation in (Request, BackgroundTasks, int):

            def invalid(value):
                return value

            invalid.__annotations__ = {"value": annotation}

            def route(value=Depends(invalid, scope="app")):
                return value

            with (
                self.subTest(annotation=annotation),
                self.assertRaisesRegex(ValueError, "application dependencies"),
            ):
                compile_inputs(route, "/")

    def test_application_dependencies_cannot_capture_request_dependencies(self):
        def request_value():
            return 1

        def application(value=Depends(request_value)):
            return value

        def handler(value=Depends(application, scope="app")):
            return value

        with self.assertRaisesRegex(ValueError, "request-scoped dependency"):
            compile_inputs(handler, "/")

    def test_aliases_and_constraints_are_declared_for_native_and_openapi(self):
        def handler(
            identifier: Annotated[int, Path(alias="id", ge=1)],
            count: Annotated[int, Query(alias="page-size", ge=1, le=100)] = 20,
            x_request_id: Annotated[str | None, Header()] = None,
            session: Annotated[str | None, Cookie(alias="sid")] = None,
        ):
            return identifier

        compiled = compile_inputs(handler, "/items/{id}")
        self.assertEqual(
            [(p["name"], p["source"], p["alias"]) for p in compiled.parameters],
            [
                ("identifier", "path", "id"),
                ("count", "query", "page-size"),
                ("x_request_id", "header", "x-request-id"),
                ("session", "cookie", "sid"),
            ],
        )
        self.assertEqual(compiled.input_validators["count"](10), 10)
        with self.assertRaises(ValueError):
            compiled.input_validators["count"](101)

    def test_invalid_metadata_and_names_fail_at_startup(self):
        invalid_parameters = [
            ("value", int, Query(ge=2, lt=2)),
            ("value", str, Query(min_length=3, max_length=2)),
            ("value", str, Header(alias="bad header")),
            ("value", str, Header(alias="é")),
            ("value", str, Cookie(alias="session;other")),
            ("value", dict, Body(embed=True)),
            ("__bessemer_request__", str, Query()),
            ("__dependency_0_value", str, Query()),
        ]
        for name, annotation, marker in invalid_parameters:

            def handler():
                return None

            handler.__signature__ = inspect.Signature(
                [
                    inspect.Parameter(
                        name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=marker
                    )
                ]
            )
            handler.__annotations__ = {name: annotation}
            with self.subTest(name=name, marker=marker), self.assertRaises(ValueError):
                compile_inputs(handler, "/")

    def test_invalid_defaults_fail_at_startup(self):
        for annotation, marker in (
            (int, Query(default=0, ge=1)),
            (str, Query(default="a", min_length=2)),
            (int, Query(default=None)),
            (float, Query(default=float("nan"))),
        ):

            def handler(value=marker):
                return value

            handler.__annotations__ = {"value": annotation}
            with (
                self.subTest(annotation=annotation, marker=marker),
                self.assertRaises(ValueError),
            ):
                compile_inputs(handler, "/")

    def test_scalar_conversion_and_literal_validation(self):
        def handler(
            identifier: UUID, day: date, colour: Colour, mode: Literal["fast", "safe"]
        ):
            return identifier

        compiled = compile_inputs(handler, "/")
        value = "d9428888-122b-11e1-b85c-61cd3cbb3210"
        self.assertEqual(compiled.input_validators["identifier"](value), UUID(value))
        self.assertEqual(
            compiled.input_validators["day"]("2026-11-04"), date(2026, 11, 4)
        )
        self.assertIs(compiled.input_validators["colour"]("red"), Colour.red)
        with self.assertRaises(ValueError):
            compiled.input_validators["mode"]("unknown")

    def test_scalar_body_validates_type_then_constraints_then_conversion(self):
        def count(value: Annotated[int, Body(ge=1)]):
            return value

        def identifier(value: Annotated[UUID, Body()]):
            return value

        compiled = compile_inputs(count, "/")
        self.assertEqual(compiled.parameters[0]["type"], "json")
        self.assertEqual(compiled.input_validators["value"](2), 2)
        for invalid in ("2", True, 0, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compiled.input_validators["value"](invalid)
        value = "d9428888-122b-11e1-b85c-61cd3cbb3210"
        self.assertEqual(
            compile_inputs(identifier, "/").input_validators["value"](value),
            UUID(value),
        )

    def test_request_injection_is_metadata_not_a_public_parameter(self):
        def handler(request: Request, tasks: BackgroundTasks, state: State):
            return None

        compiled = compile_inputs(handler, "/")
        self.assertTrue(compiled.needs_request)
        self.assertEqual(compiled.parameters, ())
        self.assertEqual(
            [b.kind for b in compiled.plan.bindings],
            ["request", "background_tasks", "state"],
        )

    def test_fallback_response_models_fail_early_when_typed_contents_need_pydantic(
        self,
    ):
        with patch.dict(sys.modules, {"pydantic": None}):
            null_adapter = response_adapter(type(None))
            self.assertIsNone(null_adapter.validate_python(None))
            self.assertEqual(null_adapter.json_schema(), {"type": "null"})
            with self.assertRaises(ValueError):
                null_adapter.validate_python("not null")
            adapter = response_adapter(list[Any])
            value = adapter.validate_python([1, "a"])
            self.assertEqual(value, [1, "a"])
            self.assertIs(adapter.dump_python(value, warnings="error"), value)
            with self.assertRaises(ValueError):
                adapter.validate_python({})
            for annotation in (list[int], dict[str, int], UUID):
                with (
                    self.subTest(annotation=annotation),
                    self.assertRaisesRegex(ValueError, "Pydantic"),
                ):
                    response_adapter(annotation)

    def test_dependency_configuration_rejects_incoherent_app_scope(self):
        for options in ({"scope": "process"}, {"scope": "app", "use_cache": False}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Depends(lambda: None, **options)


if __name__ == "__main__":
    unittest.main()

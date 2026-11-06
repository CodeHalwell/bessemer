from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Annotated
from unittest.mock import Mock, patch

import bessemer
from bessemer import App, Depends, Header, Query, Router
from bessemer.openapi import documentation_page
from bessemer.responses import PlainTextResponse

try:
    from pydantic import BaseModel, Field, create_model
except ImportError:
    BaseModel = None
else:

    class Address(BaseModel):
        postcode: str

    class Person(BaseModel):
        name: str
        address: Address

    class Tree(BaseModel):
        name: str
        children: list[Tree] = Field(default_factory=list)

    class PublicPerson(BaseModel):
        name: str = Field(serialization_alias="displayName")

    class PublicGroup(BaseModel):
        person: PublicPerson

    class SchemaNames(BaseModel):
        definitions: int = Field(alias="$defs")
        example: dict = Field(default={"$defs": {"literal": 1}})


def assert_local_refs_resolve(test, document):
    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                test.assertTrue(reference.startswith("#/"), reference)
                target = document
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                test.assertIsInstance(target, dict)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)


class RoutingTests(unittest.TestCase):
    def test_literal_unicode_routes_require_explicit_uri_encoding(self):
        for options in ({"openapi_url": "/schéma"}, {"docs_url": "/référence"}):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "ASCII"),
            ):
                App(**options)
        with self.assertRaisesRegex(ValueError, "ASCII"):
            App().get("/café")
        with self.assertRaisesRegex(ValueError, "ASCII"):
            Router(prefix="/café")
        app = App()
        app.get("/caf%C3%A9")(lambda: None)
        self.assertEqual(app._export_routes()[0]["path"], "/caf%C3%A9")

    def test_host_and_proxy_configuration_rejects_scalar_shortcuts(self):
        for name in ("allowed_hosts", "trusted_proxies"):
            for value in ("", "localhost", False, 0):
                with self.subTest(name=name, value=value), self.assertRaises(TypeError):
                    App(**{name: value})

    def test_run_forwards_bounded_response_and_supervisor_controls(self):
        serve = Mock()
        native = types.SimpleNamespace(serve=serve, Bridge=Mock())
        app = App()
        with (
            patch.dict(sys.modules, {"bessemer._native": native}),
            patch.object(bessemer, "_native", native, create=True),
            patch("bessemer.worker.Worker") as worker,
            # The descriptors below are made up; Windows cannot turn them
            # into handles.
            patch("bessemer.application._native_handle", lambda fd: fd),
        ):
            app.run(
                port=0,
                max_response_size=2048,
                max_connections=12,
                max_headers=8,
                max_header_size=8192,
                max_request_target_size=512,
                header_timeout=2,
                listener_fd=11,
                ready_fd=12,
                max_requests=50,
            )
        self.assertEqual(worker.call_args.kwargs["max_response_size"], 2048)
        self.assertEqual(worker.call_args.kwargs["heartbeat_fd"], 12)
        self.assertEqual(serve.call_args.kwargs["max_response_size"], 2048)
        self.assertEqual(serve.call_args.kwargs["listener_fd"], 11)
        self.assertEqual(serve.call_args.kwargs["max_requests"], 50)
        self.assertEqual(serve.call_args.kwargs["max_headers"], 8)
        self.assertEqual(serve.call_args.kwargs["header_timeout"], 2.0)
        self.assertEqual(serve.call_args.kwargs["controls"], app._export_settings())

    def test_tags_reject_a_single_string_in_all_registration_paths(self):
        with self.assertRaises(TypeError):
            Router(tags="users")
        with self.assertRaises(TypeError):
            App().get("/", tags="users")
        with self.assertRaises(TypeError):
            App().include_router(Router(), tags="users")

    def test_router_composes_dependencies_tags_and_operation_metadata(self):
        def global_check():
            pass

        def route_check():
            pass

        app = App(
            title="Inventory", version="1.2", dependencies=[Depends(global_check)]
        )
        router = Router(
            prefix="/items", tags=["items"], dependencies=[Depends(route_check)]
        )

        @router.get(
            "/{identifier}", summary="Read item", operation_id="readItem", tags=["read"]
        )
        def read(identifier: int):
            """Read an item by its identifier."""
            return {"id": identifier}

        app.include_router(router, prefix="/v1", tags=["v1"])
        document = app.openapi()
        operation = document["paths"]["/v1/items/{identifier}"]["get"]
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertEqual(operation["operationId"], "readItem")
        self.assertEqual(operation["tags"], ["v1", "items", "read"])
        self.assertEqual(operation["description"], "Read an item by its identifier.")
        self.assertEqual(
            [node.call for node in app._compiled[0].dependencies],
            [global_check, route_check],
        )
        self.assertEqual(len(app._compiled[0].before), 2)
        self.assertEqual(app._export_routes()[0]["tier"], 2)

    def test_schema_and_docs_are_immutable_native_assets(self):
        app = App(docs_url="/docs")

        @app.get("/")
        def index():
            return {}

        app.health()
        routes = app._export_routes()
        self.assertEqual(
            [route["kind"] for route in routes],
            ["python", "health", "static", "static"],
        )
        schema = json.loads(routes[2]["content"])
        self.assertEqual(schema, app.openapi())
        self.assertNotIn("/healthz", schema["paths"])
        self.assertNotIn("/docs", schema["paths"])
        self.assertNotIn("/openapi.json", schema["paths"])
        self.assertEqual(routes[2]["tier"], 0)
        self.assertEqual(routes[3]["content_type"], "text/html; charset=utf-8")
        schema["paths"].clear()
        self.assertIn("/", app.openapi()["paths"])
        routes[2]["content"] = b"changed"
        self.assertEqual(json.loads(app._export_routes()[2]["content"]), app.openapi())

    def test_disabled_schema_adds_no_hidden_routes(self):
        app = App(openapi_url=None)
        app.health()
        self.assertEqual(len(app._export_routes()), 1)
        self.assertEqual(app.openapi()["paths"], {})
        with self.assertRaisesRegex(ValueError, "requires"):
            App(openapi_url=None, docs_url="/docs")

    def test_documentation_assets_require_literal_unoccupied_paths(self):
        for options in (
            {"docs_url": "/docs/{name}"},
            {"openapi_url": "/schema/{name}"},
        ):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "exact paths"),
            ):
                App(**options)
        with self.assertRaises(ValueError):
            App(openapi_url="/\\example.com")
        app = App()

        @app.get("/openapi.json")
        def conflict():
            return {}

        with self.assertRaisesRegex(ValueError, "duplicate"):
            app.build()

    def test_bundled_reference_escapes_title_and_schema_path(self):
        page = documentation_page(
            "</script><script>alert(1)</script>", "/</script>"
        ).decode()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;/script&gt;", page)
        self.assertIn("\\u003c/script>", page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("https://", page)
        collision = documentation_page("__SOURCE__", "/__TITLE__").decode()
        self.assertIn("<h1>__SOURCE__</h1>", collision)
        self.assertIn('const url="/__TITLE__";', collision)

    def test_public_parameters_are_deduplicated_without_losing_aliases(self):
        def authenticate(key: Annotated[str, Header(alias="X-Api-Key")]):
            return key

        app = App()

        @app.get("/", dependencies=[Depends(authenticate)])
        def index(
            key: Annotated[str, Header(alias="x-api-key")],
            page: Annotated[int, Query(ge=1)] = 1,
        ):
            return page

        parameters = app.openapi()["paths"]["/"]["get"]["parameters"]
        self.assertEqual(len(parameters), 2)
        self.assertEqual(parameters[0]["name"], "x-api-key")
        self.assertEqual(
            parameters[1]["schema"], {"type": "integer", "minimum": 1, "default": 1}
        )

    def test_conflicting_public_parameters_fail_before_serving(self):
        def dependency(value: Annotated[int, Query(alias="shared", ge=1)]):
            return value

        app = App()

        @app.get("/", dependencies=[Depends(dependency)])
        def index(value: Annotated[int, Query(alias="shared", ge=2)]):
            return value

        with self.assertRaisesRegex(ValueError, "conflicting declarations"):
            app.build()

    def test_operation_ids_must_be_unique(self):
        app = App()

        @app.get("/first", operation_id="same")
        def first():
            return None

        @app.get("/second", operation_id="same")
        def second():
            return None

        with self.assertRaisesRegex(ValueError, "operation_id"):
            app.build()

    def test_bodyless_responses_have_no_documented_content(self):
        for method, status in (("GET", 204), ("GET", 205), ("GET", 304), ("HEAD", 200)):
            app = App()

            @app.route("/", methods=[method], status_code=status)
            def handler() -> str:
                return "ignored"

            response = app.openapi()["paths"]["/"][method.lower()]["responses"][
                str(status)
            ]
            self.assertNotIn("content", response)

    def test_explicit_response_annotations_document_media_type(self):
        app = App()

        @app.get("/")
        def index() -> PlainTextResponse:
            return PlainTextResponse("ready")

        content = app.openapi()["paths"]["/"]["get"]["responses"]["200"]["content"]
        self.assertEqual(content, {"text/plain": {"schema": {"type": "string"}}})

    def test_runtime_settings_freeze_at_build_and_exports_are_independent(self):
        config = {"requests": 5, "window_seconds": 1.0}
        app = App(rate_limit=config)
        app.build()
        config["requests"] = 100
        exported = app._export_settings()
        self.assertEqual(exported["rate_limit"]["requests"], 5)
        exported["rate_limit"]["requests"] = 200
        self.assertEqual(app._export_settings()["rate_limit"]["requests"], 5)

    def test_api_key_schema_never_contains_configuration_secret_names(self):
        app = App(api_key={"env": "PRIVATE_SECRET_ENV", "exclude_paths": ["/public"]})

        @app.get("/public")
        def public():
            return None

        @app.get("/private")
        def private():
            return None

        document = app.openapi()
        self.assertEqual(document["paths"]["/public"]["get"]["security"], [])
        self.assertEqual(
            document["paths"]["/private"]["get"]["security"], [{"APIKey": []}]
        )
        self.assertEqual(
            document["components"]["securitySchemes"]["APIKey"]["name"], "X-API-Key"
        )
        self.assertNotIn("PRIVATE_SECRET_ENV", json.dumps(document))

    def test_exception_handlers_compile_and_freeze(self):
        app = App()

        @app.exception_handler(ValueError)
        async def invalid(request, error):
            return PlainTextResponse("invalid", status_code=400)

        @app.get("/")
        def index():
            return None

        self.assertTrue(app._export_routes()[0]["needs_request"])
        self.assertEqual(app._exception_handlers[ValueError].kind, "async")
        with self.assertRaises(RuntimeError):
            app.exception_handler(KeyError)(invalid)
        with self.assertRaises(TypeError):
            App().exception_handler(BaseException)
        with self.assertRaises(ValueError):
            App().exception_handler(ValueError)(lambda error: None)

    @unittest.skipIf(BaseModel is None, "optional Pydantic v2 is not installed")
    def test_nested_nullable_recursive_models_have_resolvable_openapi_refs(self):
        app = App()

        @app.post("/people", response_model=Person)
        def person(value: Person | None = None):
            return value

        @app.post("/trees", response_model=list[Tree])
        def tree(value: Tree):
            return value

        document = app.openapi()
        assert_local_refs_resolve(self, document)
        self.assertIn("Address", document["components"]["schemas"])
        self.assertIn("Tree", document["components"]["schemas"])
        self.assertNotIn('"$defs"', json.dumps(document))

    @unittest.skipIf(BaseModel is None, "optional Pydantic v2 is not installed")
    def test_conflicting_nested_model_names_fail_at_startup(self):
        first_leaf = create_model("Leaf", count=(int, ...))
        second_leaf = create_model("Leaf", count=(str, ...))
        first = create_model("First", leaf=(first_leaf, ...))
        second = create_model("Second", leaf=(second_leaf, ...))
        app = App()
        app.get("/first", response_model=first)(lambda: None)
        app.get("/second", response_model=second)(lambda: None)
        with self.assertRaisesRegex(ValueError, "conflicting definitions"):
            app.build()

    @unittest.skipIf(BaseModel is None, "optional Pydantic v2 is not installed")
    def test_response_schema_uses_serialization_aliases_and_validation_adapter(self):
        app = App()

        @app.get("/", response_model=PublicPerson)
        def index():
            return {"name": "Ada"}

        operation = app.openapi()["paths"]["/"]["get"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertIn("displayName", schema["properties"])
        adapter = app._compiled[0].response_adapter
        self.assertEqual(
            adapter.dump_python(
                adapter.validate_python({"name": "Ada"}), mode="json", by_alias=True
            ),
            {"displayName": "Ada"},
        )
        with self.assertRaises(ValueError):
            adapter.validate_python({"missing": True})

    @unittest.skipIf(BaseModel is None, "optional Pydantic v2 is not installed")
    def test_nested_input_and_output_alias_schemas_have_distinct_namespaces(self):
        app = App()

        @app.post("/", response_model=PublicGroup)
        def group(value: PublicGroup):
            return value

        document = app.openapi()
        assert_local_refs_resolve(self, document)
        components = document["components"]["schemas"]
        self.assertIn("name", components["PublicPerson"]["properties"])
        self.assertIn("displayName", components["PublicPerson-Output"]["properties"])

    @unittest.skipIf(BaseModel is None, "optional Pydantic v2 is not installed")
    def test_schema_keyword_names_in_payload_data_are_preserved(self):
        app = App()

        @app.post("/", response_model=SchemaNames)
        def names(value: SchemaNames):
            return value

        document = app.openapi()
        schema = document["paths"]["/"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(schema["properties"]["$defs"]["type"], "integer")
        self.assertEqual(
            schema["properties"]["example"]["default"], {"$defs": {"literal": 1}}
        )
        self.assertNotIn("components", document)


if __name__ == "__main__":
    unittest.main()

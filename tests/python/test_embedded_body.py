"""Body(embed=True) and several body parameters, as in FastAPI.

Embedded body parameters are each read from their own key of one JSON object,
validated together as one generated model, and documented as FastAPI's
``Body_<operation_id>`` component.
"""

import importlib.util
import unittest
import uuid
from typing import Annotated
from unittest import mock

from bessemer import App, Body, Depends, Form, Query
from bessemer.schema import EMBEDDED_BODY_KEY, compile_inputs

PYDANTIC = importlib.util.find_spec("pydantic") is not None

if PYDANTIC:
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str
        price: float

    class User(BaseModel):
        username: str


@unittest.skipUnless(PYDANTIC, "embedded bodies need Pydantic")
class CompileTests(unittest.TestCase):
    def test_a_single_plain_body_keeps_its_own_path_and_order(self):
        def handler(first: int, item: Item, last: Annotated[int, Query()] = 1):
            return item

        compiled = compile_inputs(handler, "/")
        self.assertIsNone(compiled.embedded_body)
        self.assertEqual(
            [p["name"] for p in compiled.parameters], ["first", "item", "last"]
        )
        self.assertEqual(compiled.body_models, {"item": Item})

    def test_embedded_inputs_become_one_body_parameter_in_place(self):
        def handler(
            first: int,
            item: Item,
            user: User,
            last: Annotated[int, Query()] = 1,
        ):
            return item

        compiled = compile_inputs(handler, "/")
        self.assertEqual(
            [p["name"] for p in compiled.parameters],
            ["first", EMBEDDED_BODY_KEY, "last"],
        )
        self.assertEqual(compiled.body_models, {})
        key, model, aliases, keys = compiled.embedded_body
        self.assertEqual(key, EMBEDDED_BODY_KEY)
        self.assertEqual(aliases, ("item", "user"))
        self.assertEqual(keys, (("item", "item"), ("user", "user")))
        self.assertEqual(set(model.model_fields), {"item", "user"})

    def test_generated_field_names_do_not_collide(self):
        def handler(
            field_1: Annotated[int, Body()],
            _value: Annotated[str, Body()],
        ):  # pragma: no cover
            return field_1

        _, model, aliases, keys = compile_inputs(handler, "/").embedded_body
        self.assertEqual(aliases, ("field_1", "_value"))
        self.assertEqual(len(model.model_fields), 2)
        attributes = {name: attribute for attribute, name in keys}
        self.assertNotEqual(attributes["field_1"], attributes["_value"])
        instance = model.model_validate({"field_1": 3, "_value": "x"})
        self.assertEqual(getattr(instance, attributes["field_1"]), 3)
        self.assertEqual(getattr(instance, attributes["_value"]), "x")

    def test_constraints_on_embedded_models_fail_at_startup(self):
        def handler(
            item: Annotated[Item, Body(embed=True, min_length=1)],
        ):  # pragma: no cover
            return item

        def raw(
            data: Annotated[dict, Body(max_length=2)], user: User
        ):  # pragma: no cover
            return data

        def identifier(
            key: Annotated[uuid.UUID, Body(embed=True, min_length=1)],
        ):  # pragma: no cover
            return key

        def measured(
            value: Annotated[int, Body(embed=True, min_length=1)],
        ):  # pragma: no cover
            return value

        def compared(
            value: Annotated[str, Body(embed=True, gt=1)],
        ):  # pragma: no cover
            return value

        for function in (handler, raw, identifier, measured, compared):
            with self.subTest(handler=function.__name__), self.assertRaises(ValueError):
                compile_inputs(function, "/")

    def test_a_json_body_with_form_fields_still_fails_at_startup(self):
        def body_first(
            item: Annotated[Item, Body(embed=True)], name: Annotated[str, Form()]
        ):
            return item

        def form_first(
            name: Annotated[str, Form()], item: Item, user: User
        ):  # pragma: no cover
            return item

        for handler in (body_first, form_first):
            with self.subTest(handler=handler.__name__), self.assertRaises(ValueError):
                compile_inputs(handler, "/")

    def test_conflicting_inputs_under_one_name_fail_at_startup(self):
        def dependency(value: Annotated[int, Body()]):
            return value

        def handler(
            value: Annotated[str, Body()], other: Annotated[int, Depends(dependency)]
        ):  # pragma: no cover
            return value

        with self.assertRaises(ValueError):
            compile_inputs(handler, "/")

    def test_unsupported_marker_options_fail_at_startup(self):
        def handler(
            value: Annotated[int, Body(embed=True, include_in_schema=False)],
        ):  # pragma: no cover
            return value

        with self.assertRaises(ValueError):
            compile_inputs(handler, "/")


@unittest.skipUnless(PYDANTIC, "embedded bodies need Pydantic")
class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bessemer.testclient import TestClient

        app = App()

        @app.put("/items/{item_id}")
        async def update_item(
            item_id: int,
            item: Item,
            user: User,
            importance: Annotated[int, Body(gt=0)],
            q: str | None = None,
        ):
            return {
                "item_id": item_id,
                "item": item.model_dump(),
                "user": user.model_dump(),
                "importance": importance,
                "q": q,
            }

        @app.post("/embed")
        async def embed(item: Annotated[Item, Body(embed=True)]):
            return {"type": type(item).__name__, "item": item.model_dump()}

        @app.post("/alias")
        def alias(item: Annotated[Item, Body(embed=True, alias="it")]):
            return item.model_dump()

        @app.post("/two")
        async def two(item: Item, user: User):
            return {"item": item.model_dump(), "user": user.model_dump()}

        @app.post("/optional")
        async def optional(
            item: Item | None = None,
            count: Annotated[int, Body()] = 3,
            label: Annotated[str | None, Body(alias="Label")] = None,
        ):
            return {
                "item": None if item is None else item.model_dump(),
                "count": count,
                "label": label,
            }

        def owner(user: User) -> str:
            return user.username

        @app.post("/dependency")
        def dependency(item: Item, name: Annotated[str, Depends(owner)]):
            return {"item": item.name, "owner": name}

        def same(item: Item) -> str:
            return item.name

        @app.post("/shared")
        def shared(item: Item, name: Annotated[str, Depends(same)]):
            return {"item": item.name, "dependency": name}

        cls.app = app
        cls.client = TestClient(app).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_single_embedded_model(self):
        response = self.client.post("/embed", json={"item": {"name": "a", "price": 2}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"type": "Item", "item": {"name": "a", "price": 2.0}},
        )
        # The model's own fields at the top level are not the embedded shape.
        response = self.client.post("/embed", json={"name": "a", "price": 2})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            [error["loc"] for error in response.json()["detail"]], [["body", "item"]]
        )

    def test_two_models(self):
        body = {"item": {"name": "a", "price": 1}, "user": {"username": "u"}}
        response = self.client.post("/two", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"item": {"name": "a", "price": 1.0}, "user": {"username": "u"}},
        )

    def test_models_scalar_path_and_query_together(self):
        response = self.client.put(
            "/items/5?q=x",
            json={
                "item": {"name": "a", "price": "1.5"},
                "user": {"username": "u"},
                "importance": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "item_id": 5,
                "item": {"name": "a", "price": 1.5},
                "user": {"username": "u"},
                "importance": 2,
                "q": "x",
            },
        )

    def test_alias(self):
        response = self.client.post("/alias", json={"it": {"name": "a", "price": 2}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"name": "a", "price": 2.0})
        response = self.client.post("/alias", json={"item": {"name": "a", "price": 2}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "it"])

    def test_optional_and_default_inputs(self):
        for body in (None, {}, {"count": 3}):
            with self.subTest(body=body):
                response = self.client.post("/optional", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(), {"item": None, "count": 3, "label": None}
                )
        response = self.client.post("/optional")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"item": None, "count": 3, "label": None})
        response = self.client.post(
            "/optional",
            json={"item": {"name": "a", "price": 1}, "count": 7, "Label": "x"},
        )
        self.assertEqual(
            response.json(),
            {"item": {"name": "a", "price": 1.0}, "count": 7, "label": "x"},
        )

    def test_missing_and_invalid_fields_report_their_locations(self):
        response = self.client.put(
            "/items/5", json={"item": {"name": "a"}, "importance": 0}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            [(e["loc"], e["type"]) for e in response.json()["detail"]],
            [
                (["body", "item", "price"], "missing"),
                (["body", "user"], "missing"),
                (["body", "importance"], "greater_than"),
            ],
        )
        response = self.client.put("/items/5")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            [e["loc"] for e in response.json()["detail"]],
            [["body", "item"], ["body", "user"], ["body", "importance"]],
        )

    def test_wrong_json_shape(self):
        for body in ([1], "text", 3):
            with self.subTest(body=body):
                response = self.client.post("/two", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    [(e["loc"], e["type"]) for e in response.json()["detail"]],
                    [(["body", "item"], "missing"), (["body", "user"], "missing")],
                )
        response = self.client.post(
            "/two", content=b"{", headers={"content-type": "application/json"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["type"], "json_invalid")

    def test_dependencies_read_their_own_keys_of_one_object(self):
        response = self.client.post(
            "/dependency",
            json={"item": {"name": "a", "price": 1}, "user": {"username": "u"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"item": "a", "owner": "u"})
        # A dependency and its handler binding one name share one key.
        response = self.client.post("/shared", json={"item": {"name": "a", "price": 1}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"item": "a", "dependency": "a"})

    def test_exact_bodies_use_the_native_model_path(self):
        route = next(
            route for route in self.app._compiled if route.declaration["path"] == "/two"
        )
        model = route.embedded_body[1]
        body = {"item": {"name": "a", "price": 1.0}, "user": {"username": "u"}}
        coerced = {"item": {"name": "a", "price": "1"}, "user": {"username": "u"}}
        with mock.patch.object(
            model, "model_validate", side_effect=model.model_validate
        ) as validate:
            self.assertEqual(self.client.post("/two", json=body).status_code, 200)
            validate.assert_not_called()
            self.assertEqual(self.client.post("/two", json=coerced).status_code, 200)
            validate.assert_called_once()

    def test_openapi_documents_fastapis_body_component(self):
        document = self.client.get("/openapi.json").json()
        operation = document["paths"]["/items/{item_id}"]["put"]
        name = "Body_" + operation["operationId"]
        self.assertEqual(
            operation["requestBody"],
            {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{name}"}
                    }
                },
            },
        )
        self.assertEqual(
            document["components"]["schemas"][name],
            {
                "title": name,
                "type": "object",
                "properties": {
                    "item": {"$ref": "#/components/schemas/Item"},
                    "user": {"$ref": "#/components/schemas/User"},
                    "importance": {
                        "title": "Importance",
                        "type": "integer",
                        "exclusiveMinimum": 0,
                    },
                },
                "required": ["item", "user", "importance"],
            },
        )
        self.assertIn("Item", document["components"]["schemas"])
        self.assertEqual([p["name"] for p in operation["parameters"]], ["item_id", "q"])
        optional = document["paths"]["/optional"]["post"]
        self.assertFalse(optional["requestBody"]["required"])
        schema = document["components"]["schemas"]["Body_" + optional["operationId"]]
        self.assertNotIn("required", schema)
        self.assertEqual(
            schema["properties"]["count"],
            {"title": "Count", "type": "integer", "default": 3},
        )
        self.assertIn("Label", schema["properties"])


if __name__ == "__main__":
    unittest.main()

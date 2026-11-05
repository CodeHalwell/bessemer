"""The native Pydantic model fast path must match Pydantic exactly.

Plain models are built natively from request bodies whose values already have
their fields' exact JSON types, and returned instances are encoded natively.
Everything else falls back to Pydantic, so results, errors and response bytes
must be indistinguishable from the Pydantic path.
"""

from __future__ import annotations

import importlib.util
import json
import random
import unittest
import warnings
from typing import Literal

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None

if PYDANTIC:
    from pydantic import (
        AliasChoices,
        BaseModel,
        ConfigDict,
        Field,
        TypeAdapter,
        computed_field,
        field_serializer,
        field_validator,
        model_validator,
    )

    class Item(BaseModel):
        id: int
        name: str
        price: float
        tags: list[str]
        note: str | None = None
        count: int = 3
        active: bool = True

    class Order(BaseModel):
        items: list[Item]
        primary: Item | None = None
        matrix: list[list[float]] | None = None

    class Aliased(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int = Field(alias="Value")
        label: str = Field("x", validation_alias="in", serialization_alias="out")

    class Recursive(BaseModel):
        child: Recursive | None = None

    class Validated(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def positive(cls, value):
            if value < 0:
                raise ValueError("negative")
            return value

    class After(BaseModel):
        value: int

        @model_validator(mode="after")
        def check(self):
            return self

    class Constrained(BaseModel):
        value: int = Field(gt=0)

    class Stripped(BaseModel):
        model_config = ConfigDict(str_strip_whitespace=True)
        value: str

    class Strict(BaseModel):
        model_config = ConfigDict(strict=True)
        value: int

    class Allowing(BaseModel):
        model_config = ConfigDict(extra="allow")
        value: int

    class Choices(BaseModel):
        value: int = Field(validation_alias=AliasChoices("a", "b"))

    class Computed(BaseModel):
        value: int

        @computed_field
        @property
        def double(self) -> int:
            return self.value * 2

    class Serialized(BaseModel):
        value: int

        @field_serializer("value")
        def text(self, value):
            return str(value)

    class Private(BaseModel):
        value: int
        _secret: int = 1

    class Factory(BaseModel):
        values: list[int] = Field(default_factory=list)

    class MutableDefault(BaseModel):
        values: list[int] = []

    class Literals(BaseModel):
        kind: Literal["a", "b"]

    class Mapping(BaseModel):
        values: dict[str, int]

    class Initialised(BaseModel):
        value: int

        def __init__(self, **data):
            super().__init__(**data)

    class Revalidated(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")
        value: int

    with warnings.catch_warnings():
        # Spelled validate_by_name from Pydantic 2.11; this works on 2.10 too.
        warnings.simplefilter("ignore")

        class ByName(BaseModel):
            model_config = ConfigDict(populate_by_name=True)
            value: int = Field(alias="Value")

    class Child(Item):
        pass


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class ModelPlanTests(unittest.TestCase):
    def test_plain_models_qualify(self):
        from bessemer._model_plan import model_plan

        for model in (Item, Order, Aliased):
            with self.subTest(model=model.__name__):
                plan = model_plan(model)
                self.assertIsNotNone(plan)
                self.assertIs(plan.model, model)

    def test_anything_pydantic_must_run_keeps_the_pydantic_path(self):
        from bessemer._model_plan import model_plan

        for model in (
            Recursive,
            Validated,
            After,
            Constrained,
            Stripped,
            Strict,
            Allowing,
            Choices,
            Computed,
            Serialized,
            Private,
            Factory,
            MutableDefault,
            Literals,
            Mapping,
            Initialised,
            Revalidated,
            ByName,
            dict,
            int,
            None,
        ):
            with self.subTest(model=getattr(model, "__name__", model)):
                self.assertIsNone(model_plan(model))


def _same(test, fast, slow):
    test.assertIs(type(fast), type(slow))
    if isinstance(slow, BaseModel):
        test.assertEqual(list(fast.__dict__), list(slow.__dict__))
        test.assertEqual(fast.model_fields_set, slow.model_fields_set)
        test.assertEqual(fast.__pydantic_extra__, slow.__pydantic_extra__)
        test.assertEqual(fast.__pydantic_private__, slow.__pydantic_private__)
        for key in slow.__dict__:
            _same(test, fast.__dict__[key], slow.__dict__[key])
    elif isinstance(slow, list):
        test.assertEqual(len(fast), len(slow))
        for left, right in zip(fast, slow, strict=True):
            _same(test, left, right)
    elif isinstance(slow, float):
        test.assertEqual(fast.hex(), slow.hex())
    else:
        test.assertEqual(fast, slow)


def _random_value(rng):
    return rng.choice(
        [
            0,
            -7,
            2**63 - 1,
            2**63,
            2**64,
            2**70,
            1.0,
            -0.0,
            2.5,
            1e300,
            1e-7,
            0.1,
            "",
            "text",
            "12",
            "1.5",
            "true",
            "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
            True,
            False,
            None,
            [],
            ["a", "b"],
            [1, 2.5],
            [[1.5, 2], []],
            {},
            {"id": 1},
        ]
    )


def _random_item(rng):
    item = {
        "id": rng.choice([1, 2**40, -3]),
        "name": "n",
        "price": rng.choice([1, 1.25, 10**15 + 1]),
        "tags": ["a"],
    }
    if rng.random() < 0.3:
        item["note"] = rng.choice([None, "x"])
    if rng.random() < 0.3:
        item["count"] = 9
    if rng.random() < 0.3:
        item["active"] = False
    if rng.random() < 0.4:
        key = rng.choice([*item, "count", "note", "active", "extra"])
        item[key] = _random_value(rng)
    if rng.random() < 0.1 and item:
        del item[rng.choice(list(item))]
    return item


def _random_order(rng):
    order = {"items": [_random_item(rng) for _ in range(rng.randrange(4))]}
    if rng.random() < 0.3:
        order["primary"] = rng.choice([None, _random_item(rng)])
    if rng.random() < 0.3:
        order["matrix"] = rng.choice([None, [[1, 2.5], []], [[1, "x"]], [1]])
    if rng.random() < 0.1:
        order = rng.choice([[], None, "x", {"items": None}])
    return order


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class BuildTests(unittest.TestCase):
    def setUp(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan

        self.native = _native
        self.plan = model_plan

    def check(self, model, body):
        """Return whether the fast path built it; assert it matches Pydantic."""
        data = json.dumps(body).encode()
        fast = self.native._build_model(self.plan(model), data)
        if fast is None:
            return False
        slow = model.model_validate(self.native._parse_json(data))
        _same(self, fast, slow)
        return True

    def test_exact_bodies_build_the_same_instances_as_pydantic(self):
        body = {
            "items": [
                {"id": 1, "name": "a", "price": 2, "tags": []},
                {
                    "id": 2,
                    "name": "b",
                    "price": 2.5,
                    "tags": ["x"],
                    "note": None,
                    "count": 1,
                    "active": False,
                },
            ],
            "primary": {"id": 3, "name": "c", "price": 0.1, "tags": ["y", "z"]},
            "matrix": [[1, 2.5], []],
            "ignored": {"anything": [1]},
        }
        self.assertTrue(self.check(Order, body))
        self.assertTrue(self.check(Aliased, {"Value": 1, "in": "y"}))
        self.assertTrue(self.check(Aliased, {"Value": 1}))

    def test_bodies_that_need_coercion_or_errors_fall_back(self):
        item = {"id": 1, "name": "a", "price": 2, "tags": []}
        for body in (
            {"items": [item | {"id": "1"}]},
            {"items": [item | {"id": 1.0}]},
            {"items": [item | {"id": True}]},
            {"items": [item | {"id": 2**64}]},
            {"items": [item | {"price": "2"}]},
            {"items": [item | {"price": 2**64}]},
            {"items": [item | {"name": 1}]},
            {"items": [item | {"tags": "a"}]},
            {"items": [item | {"tags": [1]}]},
            {"items": [item | {"active": 1}]},
            {"items": [{"name": "a", "price": 2, "tags": []}]},
            {"items": None},
            {"items": {}},
            {"items": [None]},
            [],
            None,
        ):
            with self.subTest(body=body):
                self.assertFalse(self.check(Order, body))
        self.assertFalse(self.check(Aliased, {"value": 1}))
        self.assertFalse(self.check(Aliased, {"Value": 1, "extra": 1}))
        self.assertFalse(self.check(Aliased, {"Value": 1, "label": "x"}))

    def test_random_bodies_match_pydantic(self):
        rng = random.Random(41)
        built = 0
        for _ in range(3000):
            body = _random_order(rng)
            with self.subTest(body=body):
                built += self.check(Order, body)
        # Both outcomes are exercised.
        self.assertGreater(built, 300)
        self.assertLess(built, 2700)


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class EncodeTests(unittest.TestCase):
    def setUp(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan

        self.native = _native
        self.plan = model_plan

    def pydantic_bytes(self, model, value, limit=1 << 20):
        dumped = TypeAdapter(model).dump_python(
            value, mode="json", by_alias=True, warnings="error"
        )
        return self.native._encode_plain_json(dumped, limit)

    def test_encoding_matches_pydantic_and_the_plain_encoder(self):
        rng = random.Random(7)
        encoded = 0
        for _ in range(2000):
            body = _random_order(rng)
            try:
                value = Order.model_validate(body)
            except ValueError:
                continue
            fast = self.native._encode_model_json(self.plan(Order), value, 1 << 20)
            if fast is not None:
                encoded += 1
                self.assertEqual(fast, self.pydantic_bytes(Order, value))
        self.assertGreater(encoded, 300)
        value = Aliased.model_validate({"Value": 1, "in": 'q"\N{SNOWMAN}\n'})
        self.assertEqual(
            self.native._encode_model_json(self.plan(Aliased), value, 1 << 20),
            self.pydantic_bytes(Aliased, value),
        )
        self.assertEqual(
            json.loads(self.pydantic_bytes(Aliased, value)),
            {"Value": 1, "out": 'q"\N{SNOWMAN}\n'},
        )

    def test_values_pydantic_must_serialize_fall_back(self):
        plan = self.plan(Item)
        encode = self.native._encode_model_json

        def item(**changes):
            value = Item(id=1, name="a", price=1.5, tags=["x"])
            for key, change in changes.items():
                object.__setattr__(value, key, change)
            return value

        for value in (
            item(id="1"),
            item(id=True),
            item(id=2**64),
            item(price=1),
            item(price=float("nan")),
            item(price=float("inf")),
            item(name="\ud800"),
            item(tags=("x",)),
            item(tags=[1]),
            item(note=b"x"),
            item(active=1),
            Child(id=1, name="a", price=1.5, tags=["x"]),
            {"id": 1},
        ):
            with self.subTest(value=value):
                self.assertIsNone(encode(plan, value, 1 << 20))
        value = item()
        del value.__dict__["note"]
        self.assertIsNone(encode(plan, value, 1 << 20))
        value = item()
        value.__dict__["other"] = 1
        self.assertIsNone(encode(plan, value, 1 << 20))
        value = item()
        exact = encode(plan, value, 1 << 20)
        self.assertEqual(encode(plan, value, len(exact)), exact)
        self.assertIsNone(encode(plan, value, len(exact) - 1))

    def test_encoding_while_another_thread_mutates_the_instance(self):
        import threading

        plan = self.plan(Order)
        value = Order.model_validate(
            {"items": [{"id": 1, "name": "a", "price": 1.5, "tags": ["x"]}]}
        )
        stop = threading.Event()

        def mutate():
            item = value.items[0]
            while not stop.is_set():
                item.tags.append("y")
                object.__setattr__(item, "name", "b" * 20)
                item.tags.clear()
                object.__setattr__(item, "price", 2)
                value.items.append(item)
                object.__setattr__(item, "price", 2.5)
                del value.items[1:]

        thread = threading.Thread(target=mutate)
        thread.start()
        try:
            for _ in range(20000):
                encoded = self.native._encode_model_json(plan, value, 1 << 20)
                if encoded is not None:
                    self.assertIsInstance(json.loads(encoded)["items"], list)
        finally:
            stop.set()
            thread.join()


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class HTTPTests(unittest.TestCase):
    """Through the real server: same responses with and without the fast path."""

    @classmethod
    def setUpClass(cls):
        from bessemer import App
        from bessemer.testclient import TestClient

        app = App()
        seen = []

        @app.post("/echo")
        async def echo(order: Order) -> Order:
            seen.append(order)
            return order

        @app.post("/forbid")
        async def forbid(value: Aliased) -> Aliased:
            return value

        @app.post("/optional")
        async def optional(order: Order | None = None) -> dict:
            return {"none": order is None}

        @app.post("/changed")
        async def changed(order: Order) -> Order:
            order.items = "not a list"
            return order

        @app.post("/plain", response_model=Item)
        async def plain(order: Order):
            return order.items[0].model_dump()

        cls.seen = seen
        cls.client = TestClient(app).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_valid_bodies_round_trip(self):
        body = {
            "items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}],
            "primary": None,
        }
        response = self.client.post("/echo", json=body)
        self.assertEqual(response.status_code, 200)
        expected = Order.model_validate(body)
        self.assertEqual(response.json(), expected.model_dump(mode="json"))
        self.assertEqual(self.seen[-1], expected)
        self.assertEqual(self.seen[-1].model_fields_set, expected.model_fields_set)
        coerced = {"items": [{"id": "1", "name": "a", "price": "2", "tags": []}]}
        response = self.client.post("/echo", json=coerced)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), Order.model_validate(coerced).model_dump(mode="json")
        )
        # Keys are written in byte order, as the plain encoder writes them.
        self.assertEqual(
            response.content,
            json.dumps(
                Order.model_validate(coerced).model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )

    def test_only_inexact_bodies_reach_pydantic(self):
        from unittest import mock

        exact = {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]}
        coerced = {"items": [{"id": "1", "name": "a", "price": 2, "tags": ["x"]}]}
        with mock.patch.object(
            Order, "model_validate", side_effect=Order.model_validate
        ) as validate:
            self.assertEqual(self.client.post("/echo", json=exact).status_code, 200)
            validate.assert_not_called()
            self.assertEqual(self.client.post("/echo", json=coerced).status_code, 200)
            validate.assert_called_once()

    def test_errors_are_pydantics(self):
        for body in (
            {"items": [{"id": "x", "name": "a", "price": 2, "tags": []}]},
            {"items": [{"name": "a", "price": 2, "tags": []}]},
            {"items": "x"},
            [],
        ):
            with self.subTest(body=body):
                response = self.client.post("/echo", json=body)
                self.assertEqual(response.status_code, 422)
                detail = response.json()["detail"]
                with self.assertRaises(ValueError) as caught:
                    Order.model_validate(body)
                self.assertEqual(
                    [(item["loc"], item["type"]) for item in detail],
                    [
                        (["body", *item["loc"]], item["type"])
                        for item in caught.exception.errors()
                    ],
                )
        response = self.client.post("/forbid", json={"Value": 1, "other": 2})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["type"], "extra_forbidden")
        response = self.client.post("/echo", json=None)
        self.assertEqual(response.status_code, 422)

    def test_optional_and_dict_responses(self):
        self.assertEqual(self.client.post("/optional").json(), {"none": True})
        self.assertEqual(
            self.client.post("/optional", json={"items": []}).json(), {"none": False}
        )
        body = {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]}
        response = self.client.post("/plain", json=body)
        self.assertEqual(response.json(), Item(**body["items"][0]).model_dump())

    def test_an_invalid_returned_instance_still_fails_like_pydantic(self):
        body = {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]}
        response = self.client.post("/changed", json=body)
        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()

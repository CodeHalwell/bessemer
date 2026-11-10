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
from typing import Annotated, Any, Literal

from bessemer import Body

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

    class OutOfOrder(BaseModel):
        # Declaration order is not alphabetical, and the middle field's
        # serialization alias differs from its name.
        zebra: str
        alpha: int
        middle: list[int] = []
        first: Item | None = Field(None, serialization_alias="FIRST")

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

    class NestedMutableDefault(BaseModel):
        id: int
        values: list[str] = ["a", "b"]

    class TupleDefault(BaseModel):
        values: tuple[int, ...] = ()

    class ObjectDefault(BaseModel):
        value: object = object()

    class MismatchedDefault(BaseModel):
        value: int = "x"
        other: float = []

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

    class Checked(Item):
        @classmethod
        def model_validate(cls, value, **options):
            if value.get("id", 0) < 0:
                raise ValueError("negative id")
            return super().model_validate(value, **options)

    construction_events = []

    class ConstructedItem(BaseModel):
        value: int

        def __new__(cls, *args, **kwargs):
            construction_events.append("item")
            return super().__new__(cls)

    class ConstructedEnvelope(BaseModel):
        items: list[ConstructedItem]

        def __new__(cls, *args, **kwargs):
            construction_events.append("envelope")
            return super().__new__(cls)

    class InheritedConstructedItem(ConstructedItem):
        pass

    class InheritedConstructedEnvelope(BaseModel):
        items: list[InheritedConstructedItem]

    class FinalizedItem(BaseModel):
        value: int

        def __del__(self):
            construction_events.append("finalized")

    class InheritedFinalizedItem(FinalizedItem):
        pass

    class FinalizedEnvelope(BaseModel):
        items: list[InheritedFinalizedItem]


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class ModelPlanTests(unittest.TestCase):
    def test_plain_models_qualify(self):
        from bessemer._model_plan import model_plan

        for model in (Item, Order, Aliased):
            with self.subTest(model=model.__name__):
                plan = model_plan(model)
                self.assertIsNotNone(plan)
                self.assertIs(plan.model, model)

    def test_models_nested_past_the_native_limit_keep_the_pydantic_path(self):
        from bessemer._model_plan import model_plan
        from pydantic import create_model

        model = create_model("Level0", value=(int, 0))
        for level in range(1, 40):
            model = create_model(f"Level{level}", child=(model | None, None))
        self.assertIsNone(model_plan(model))
        self.assertIsNotNone(model_plan(create_model("Shallow", child=(Item, None))))

    def test_custom_construction_and_finalization_keep_the_pydantic_path(self):
        from bessemer._model_plan import model_plan

        for model in (
            ConstructedItem,
            ConstructedEnvelope,
            InheritedConstructedItem,
            InheritedConstructedEnvelope,
            FinalizedItem,
            InheritedFinalizedItem,
            FinalizedEnvelope,
        ):
            with self.subTest(model=model.__name__):
                self.assertIsNone(model_plan(model))

    def test_json_default_defaults_qualify(self):
        from bessemer._model_plan import model_plan

        with self.subTest(model="mutable default"):
            self.assertIsNotNone(model_plan(MutableDefault))
        with self.subTest(model="nested mutable default"):
            self.assertIsNotNone(model_plan(NestedMutableDefault))
        with self.subTest(model="default factory"):
            self.assertIsNone(model_plan(Factory))
        with self.subTest(model="tuple default"):
            self.assertIsNone(model_plan(TupleDefault))
        with self.subTest(model="object default"):
            self.assertIsNone(model_plan(ObjectDefault))
        with self.subTest(model="mismatched default"):
            self.assertIsNone(model_plan(MismatchedDefault))

    def test_mutable_defaults_are_fresh_per_instance_like_pydantic(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan

        plan = model_plan(MutableDefault)
        first = _native._parse_model(plan, b"{}")
        second = _native._parse_model(plan, b"{}")
        slow = MutableDefault.model_validate({})
        _same(self, first, slow)
        _same(self, second, slow)
        self.assertIsNot(first.values, second.values)
        self.assertIsNot(first.values, slow.values)
        first.values.append("changed")
        self.assertEqual(second.values, [])
        self.assertEqual(slow.values, [])
        self.assertEqual(first.model_fields_set, slow.model_fields_set)
        self.assertEqual(second.model_fields_set, slow.model_fields_set)

    def test_mutable_default_bytes_encode_the_same(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan
        from pydantic import TypeAdapter

        plan = model_plan(list[NestedMutableDefault])
        adapter = TypeAdapter(list[NestedMutableDefault])
        value = [{"id": 1}, {"id": 2, "values": ["x"]}]
        self.assertEqual(
            _native._encode_model_list_json(plan, value, 1 << 20),
            _native._encode_plain_json(
                adapter.dump_python(
                    adapter.validate_python(value),
                    mode="json",
                    by_alias=True,
                    warnings="error",
                ),
                1 << 20,
            ),
        )

    def test_response_model_lists_are_described_by_a_list_plan(self):
        from bessemer import _native
        from bessemer._model_plan import _MAX_RESPONSE_DEPTH, model_plan
        from pydantic import TypeAdapter

        self.assertIs(model_plan(list[Order]).model, Order)
        self.assertTrue(model_plan(list[Order]).is_list)
        # Nested lists, optionals (either spelling) and string-keyed dicts now
        # compile too; the values' model is still the plan's model.
        nested = model_plan(list[list[Item]])
        self.assertTrue(nested.is_list)
        self.assertIs(nested.model, Item)
        for shape in (Order | None, Order | type(None), type(None) | Order):
            plan = model_plan(shape)
            self.assertIsNotNone(plan)
            self.assertTrue(plan.is_optional)
            self.assertFalse(plan.is_list)
            self.assertIs(plan.model, Order)
        strdict = model_plan(dict[str, Order])
        self.assertIsNotNone(strdict)
        self.assertFalse(strdict.is_list)
        self.assertFalse(strdict.is_optional)
        self.assertIs(strdict.model, Order)
        self.assertIsNone(model_plan(dict[int, Order]))
        self.assertIsNone(model_plan(list[str]))
        self.assertIsNone(model_plan("Order"))
        deep: Any = Item
        for _ in range(_MAX_RESPONSE_DEPTH + 1):
            deep = list[deep]
        self.assertIsNone(model_plan(deep))

        model, value = (
            list[Order],
            [
                {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]},
                {"items": []},
            ],
        )
        self.assertEqual(
            _native._encode_model_list_json(model_plan(model), value, 1 << 20),
            _native._encode_plain_json(
                TypeAdapter(model).dump_python(
                    TypeAdapter(model).validate_python(value),
                    mode="json",
                    by_alias=True,
                    warnings="error",
                ),
                1 << 20,
            ),
        )
        # Instances of the model are kept, dicts are validated, other items
        # and invalid items leave the whole list to Pydantic.
        built = Order.model_validate(value[0])
        one = _native._encode_model_list_json(model_plan(model), [value[0]], 1 << 20)
        self.assertEqual(
            _native._encode_model_list_json(model_plan(model), [built], 1 << 20), one
        )
        item = one[1:-1]
        self.assertEqual(
            _native._encode_model_list_json(
                model_plan(model), [built, value[0]], 1 << 20
            ),
            b"[" + item + b"," + item + b"]",
        )
        for broken in (
            [value[0], {"items": [{"id": "x"}]}],
            [("not", "a", "dict")],
            None,
            tuple(value),
            {},
        ):
            with self.subTest(broken=broken):
                self.assertIsNone(
                    _native._encode_model_list_json(model_plan(model), broken, 1 << 20)
                )

    def test_list_model_encoding_matches_pydantic_and_the_plain_encoder(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan
        from pydantic import TypeAdapter

        rng = random.Random(11)
        plan = model_plan(list[Item])
        adapter = TypeAdapter(list[Item])
        encoded = 0
        for _ in range(3000):
            value = [_random_item(rng) for _ in range(rng.randrange(4))]
            try:
                validated = adapter.validate_python(value)
            except ValueError:
                continue
            fast = _native._encode_model_list_json(plan, value, 1 << 20)
            if fast is not None:
                encoded += 1
                self.assertEqual(
                    fast,
                    _native._encode_plain_json(
                        adapter.dump_python(
                            validated, mode="json", by_alias=True, warnings="error"
                        ),
                        1 << 20,
                    ),
                )
        self.assertGreater(encoded, 300)

    def test_wrapped_response_plans_match_pydantic_bytes(self):
        """Optional, nested-list and dict-shaped response_model annotations
        encode byte-for-byte what TypeAdapter.dump_json emits."""
        from bessemer import _native
        from bessemer._model_plan import model_plan
        from pydantic import TypeAdapter

        adapter_options = {"by_alias": True}

        def expected(annotation, value):
            adapter = TypeAdapter(annotation)
            return adapter.dump_json(adapter.validate_python(value), **adapter_options)

        def check(annotation, value):
            with self.subTest(annotation=str(annotation), value=value):
                plan = model_plan(annotation)
                self.assertIsNotNone(plan)
                fast = _native._encode_model_response_json(plan, value, 1 << 20)
                self.assertEqual(fast, expected(annotation, value))
                return fast

        order = {
            "items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}],
            "primary": None,
        }
        # Optional: None writes null; a dict value is validated and written.
        check(Order | None, None)
        check(Order | type(None), order)
        check(type(None) | Order, order)
        # An instance keeps its class's fields in declaration order.
        check(Order | None, Order.model_validate(order))
        # A None inside a list-of-optionals.
        check(list[Order | None], [None, order, None])
        # Nested lists with dict values.
        check(
            list[list[Item]],
            [
                [
                    {"id": 1, "name": "a", "price": 2, "tags": []},
                    {"id": 2, "name": "b", "price": 1.5, "tags": []},
                ],
                [],
            ],
        )
        # A string-keyed dict keeps the dict's key order; nested dict values,
        # defaults and aliases follow the model.
        check(
            dict[str, Item],
            {
                "zebra": {"id": 1, "name": "a", "price": 2, "tags": []},
                "alpha": {"id": 2, "name": "b", "price": 1.5, "tags": ["t"]},
            },
        )
        check(dict[str, Aliased], {"k": {"Value": 1, "in": "y"}})
        # Wrappers compose: optional of list of optionals.
        check(dict[str, list[Order | None]], {"k": [None, order]})

    def test_wrapped_responseShapes_that_do_not_match_fall_back(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan

        for annotation, value in (
            (Order | None, {"items": [{"id": "x"}]}),
            (Order | None, ()),
            (Order | None, 1),
            (Order | None, Order.__subclasses__()),
            (list[Order | None], [{}]),  # items missing their required fields
            (list[list[Item]], [[{"id": "x"}]]),
            (list[list[Item]], ("not", "a", "list")),
            (dict[str, Item], {1: {"id": 1, "name": "a", "price": 1.5, "tags": []}}),
            (
                dict[str, Item],
                [("k", {"id": 1, "name": "a", "price": 1.5, "tags": []})],
            ),
            (dict[str, Item], {"k": {"id": "x"}}),
        ):
            # A dict keyed by something other than str is not compiled at all:
            # Pydantic's own key coercions and errors stay the only path.
            with self.subTest(annotation=str(annotation), value=value):
                plan = model_plan(annotation)
                self.assertIsNotNone(plan)
                self.assertIsNone(
                    _native._encode_model_response_json(plan, value, 1 << 20)
                )

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
            {"items": [], "primary": {"id": 1}},
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


def _random_text(rng, body):
    """Serialise ``body`` with random spacing and escaping."""
    text = json.dumps(
        body,
        ensure_ascii=rng.random() < 0.5,
        separators=rng.choice([(",", ":"), (", ", ": "), (" ,\n", " :\t")]),
        indent=rng.choice([None, None, 2]),
    )
    if rng.random() < 0.3:
        text = text.replace('"name"', '"n\\u0061me"')
    if rng.random() < 0.2:
        text = " \r\n" + text + "\t "
    return text.encode()


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class ParseTests(unittest.TestCase):
    """Parsing bytes straight into a model agrees with parsing then building."""

    def setUp(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan

        self.native = _native
        self.plan = model_plan(Order)

    def check(self, data):
        try:
            expected = self.native._build_model(self.plan, data)
        except ValueError:
            with self.assertRaises(ValueError):
                self.native._parse_model(self.plan, data)
            return None
        fast = self.native._parse_model(self.plan, data)
        if fast is None:
            return False
        self.assertIsNotNone(expected)
        _same(self, fast, expected)
        return True

    def test_random_bodies_match_the_value_path(self):
        rng = random.Random(45)
        built = 0
        for _ in range(3000):
            data = _random_text(rng, _random_order(rng))
            with self.subTest(data=data):
                if self.check(data):
                    built += 1
                elif self.native._build_model(self.plan, data) is not None:
                    self.fail("the value path built a model the parser refused")
        self.assertGreater(built, 300)
        self.assertLess(built, 2700)

    def test_invalid_json_is_refused_like_the_value_path(self):
        rng = random.Random(46)
        refused = 0
        for _ in range(3000):
            data = bytearray(_random_text(rng, _random_order(rng)))
            for _ in range(rng.randrange(1, 3)):
                index = rng.randrange(len(data) + 1)
                choice = rng.random()
                if choice < 0.4 and data:
                    del data[min(index, len(data) - 1)]
                elif choice < 0.7:
                    data[index:index] = rng.choice(
                        [b",", b"}", b"]", b'"', b"\\", b"\xff", b"\x00", b"1", b"-"]
                    )
                else:
                    data = data[:index]
            with self.subTest(data=bytes(data)):
                if self.check(bytes(data)) is None:
                    refused += 1
        self.assertGreater(refused, 500)

    def test_duplicate_keys_fall_back(self):
        item = b'{"id": 1, "name": "a", "price": 2, "tags": []}'
        data = b'{"items": [], "items": [' + item + b"]}"
        self.assertIsNone(self.native._parse_model(self.plan, data))
        built = self.native._build_model(self.plan, data)
        self.assertEqual(len(built.items), 1)

    def test_model_field_sets_are_independent(self):
        data = (
            b'{"items":[{"id":1,"name":"a","price":1.5,"tags":[]},'
            b'{"id":2,"name":"b","price":2.5,"tags":[],"note":"x"}]}'
        )
        for build in (self.native._parse_model, self.native._build_model):
            with self.subTest(build=build.__name__):
                first = build(self.plan, data)
                second = build(self.plan, data)
                self.assertEqual(
                    first.items[0].model_fields_set,
                    {"id", "name", "price", "tags"},
                )
                self.assertEqual(
                    first.items[1].model_fields_set,
                    {"id", "name", "price", "tags", "note"},
                )
                first.items[0].model_fields_set.clear()
                self.assertEqual(
                    second.items[0].model_fields_set,
                    {"id", "name", "price", "tags"},
                )

    def test_defaulted_models_keep_order_and_fresh_values(self):
        from bessemer._model_plan import model_plan
        from pydantic import create_model

        plan = model_plan(Item)
        data = b'{"id":1,"name":"a","price":1.5,"tags":[]}'
        first = self.native._parse_model(plan, data)
        second = self.native._parse_model(plan, data)
        self.assertEqual(
            list(first.__dict__),
            ["id", "name", "price", "tags", "note", "count", "active"],
        )
        self.assertIsNot(first.__dict__, second.__dict__)
        first.__dict__["id"] = 99
        self.assertEqual(second.id, 1)
        self.assertEqual(first.model_fields_set, {"id", "name", "price", "tags"})

        with_note = self.native._parse_model(
            plan, b'{"id":2,"name":"b","price":2.5,"tags":[],"note":"set"}'
        )
        self.assertEqual(
            with_note.model_fields_set, {"id", "name", "price", "tags", "note"}
        )
        defaults = create_model("Defaults", value=(int, 1), label=(str, "x"))
        empty = self.native._parse_model(model_plan(defaults), b"{}")
        self.assertEqual(list(empty.__dict__), ["value", "label"])
        self.assertEqual(empty.model_fields_set, set())

    def test_model_allocation_matches_pydantic_across_field_counts(self):
        from bessemer._model_plan import model_plan
        from pydantic import create_model

        for count in (0, 1, 4, 16, 17, 20, 32):
            with self.subTest(count=count):
                model = create_model(
                    f"Fields{count}",
                    **{f"field_{index}": (int, ...) for index in range(count)},
                )
                body = {f"field_{index}": index for index in range(count)}
                data = json.dumps(body).encode()
                plan = model_plan(model)
                self.assertIsNotNone(plan)
                expected = model.model_validate(body)
                _same(self, self.native._parse_model(plan, data), expected)
                _same(self, self.native._build_model(plan, data), expected)
                if count:
                    missing = dict(body)
                    missing.pop("field_0")
                    self.assertIsNone(
                        self.native._parse_model(plan, json.dumps(missing).encode())
                    )

    def test_nested_models_between_other_fields_keep_their_own_values(self):
        """Models inside models, before, between and after their siblings.

        The parser expects each model's keys in its field order and searches
        for any that come otherwise. Each model has its own place in that
        order, so a nested model must neither lose its parent's nor inherit
        it, in any key order and with keys left out.
        """
        from bessemer._model_plan import model_plan
        from pydantic import create_model

        # Built from type objects: this module's annotations are strings,
        # which cannot name classes local to a test.
        leaf_model = create_model("Leaf", a=(int, ...), b=(str, "b"))
        wide_model = create_model(
            "Wide",
            **{f"w{index}": (int, index) for index in range(24)},
            leaf=(leaf_model, ...),
        )
        middle_model = create_model(
            "Middle",
            before=(int, ...),
            leaf=(leaf_model, ...),
            leaves=(list[leaf_model], ...),
            wide=(wide_model | None, None),
            after=(str, "after"),
        )
        outer_model = create_model(
            "Outer",
            first=(int, ...),
            middle=(middle_model, ...),
            middles=(list[middle_model], ...),
            maybe=(middle_model | None, None),
            last=(float, ...),
        )

        def leaf(rng):
            value = {"a": rng.randrange(100)}
            if rng.random() < 0.5:
                value["b"] = rng.choice(["x", "é", ""])
            return value

        def middle(rng):
            value = {
                "before": rng.randrange(100),
                "leaf": leaf(rng),
                "leaves": [leaf(rng) for _ in range(rng.randrange(4))],
            }
            if rng.random() < 0.5:
                keys = rng.sample(range(24), rng.randrange(25))
                value["wide"] = {f"w{index}": -index for index in keys}
                value["wide"]["leaf"] = leaf(rng)
            if rng.random() < 0.5:
                value["after"] = "set"
            return value

        def shuffled(rng, value):
            if isinstance(value, dict):
                keys = list(value)
                rng.shuffle(keys)
                return {key: shuffled(rng, value[key]) for key in keys}
            if isinstance(value, list):
                return [shuffled(rng, item) for item in value]
            return value

        plan = model_plan(outer_model)
        self.assertIsNotNone(plan)
        rng = random.Random(53)
        for _ in range(500):
            body = {
                "first": rng.randrange(100),
                "middle": middle(rng),
                "middles": [middle(rng) for _ in range(rng.randrange(4))],
                "last": rng.random(),
            }
            if rng.random() < 0.5:
                body["maybe"] = rng.choice([None, middle(rng)])
            body = shuffled(rng, body)
            data = _random_text(rng, body)
            with self.subTest(data=data):
                expected = outer_model.model_validate(body)
                _same(self, self.native._parse_model(plan, data), expected)
                _same(self, self.native._build_model(plan, data), expected)
        # A nested model that cannot be built natively refuses the whole
        # body, wherever it sits, and the next body still parses.
        good = {"first": 1, "middle": middle(rng), "middles": [], "last": 0.5}
        for path in (("middle", "leaf"), ("middle",)):
            broken = json.loads(json.dumps(good))
            target = broken
            for key in path:
                target = target[key]
            target.pop("a" if path[-1] == "leaf" else "before")
            self.assertIsNone(
                self.native._parse_model(plan, json.dumps(broken).encode())
            )
            _same(
                self,
                self.native._parse_model(plan, json.dumps(good).encode()),
                outer_model.model_validate(good),
            )

    def test_thousand_nested_items_match_pydantic(self):
        data = json.dumps(
            {
                "items": [
                    {
                        "id": i,
                        "name": f"item-{i}",
                        "price": i * 1.25,
                        "tags": ["a", "b"],
                    }
                    for i in range(1000)
                ]
            }
        ).encode()
        fast = self.native._parse_model(self.plan, data)
        self.assertIsNotNone(fast)
        self.assertEqual(len(fast.items), 1000)
        _same(self, fast, Order.model_validate_json(data))

    def test_depth_limit_matches(self):
        for depth in (60, 63, 64, 65, 200):
            nested = b"[" * depth + b"]" * depth
            data = b'{"items": [], "ignored": ' + nested + b"}"
            with self.subTest(depth=depth):
                self.check(data)

    def test_trailing_content_is_refused(self):
        for data in (b'{"items": []} x', b'{"items": []}{}', b'{"items": []},'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.native._parse_model(self.plan, data)


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
        # An exact int carries a float field's value the way Pydantic
        # serialises it (a float): 1 is written 1.0, with no warning.
        one = item(price=1)
        self.assertEqual(encode(plan, one, 1 << 20), self.pydantic_bytes(Item, one))
        value = item()
        del value.__dict__["note"]
        self.assertIsNone(encode(plan, value, 1 << 20))
        value = item()
        value.__dict__["other"] = 1
        self.assertIsNone(encode(plan, value, 1 << 20))

        class Key(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                return str.__eq__(self, other)

        value = item()
        del value.__dict__["note"]
        value.__dict__[Key("note")] = None
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

        first_order = {
            "items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}],
            "primary": None,
        }

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

        @app.post("/constrained")
        async def constrained(value: Annotated[Aliased, Body(min_length=1)]) -> Aliased:
            return value

        @app.post("/checked")
        async def checked(value: Checked) -> dict:
            return {"id": value.id}

        @app.post("/plain", response_model=Item)
        async def plain(order: Order):
            return order.items[0].model_dump()

        @app.get("/models", response_model=list[Item])
        async def models():
            return [
                {"id": index, "name": "x", "price": 1.5, "tags": ["a"]}
                for index in range(3)
            ]

        @app.post("/models_post", response_model=list[Item])
        async def models_post(order: Order):
            return [item.model_dump() for item in order.items]

        @app.get("/optional_model", response_model=Order | None)
        async def optional_model(absent: bool = False):
            if absent:
                return None
            return first_order

        @app.get("/nested_lists", response_model=list[list[Item]])
        async def nested_lists():
            return [
                [
                    {"id": 1, "name": "a", "price": 2, "tags": ["x"]},
                    {"id": 2, "name": "b", "price": 1.5, "tags": []},
                ],
                [],
            ]

        @app.get("/model_dict", response_model=dict[str, OutOfOrder])
        async def model_dict():
            return {
                "zebra": {"zebra": "s", "alpha": 1, "middle": [1]},
                "alpha": {"zebra": "t", "alpha": 2},
            }

        @app.get("/out_of_order", response_model=list[OutOfOrder])
        async def out_of_order():
            return [
                {"zebra": "z", "alpha": 2, "middle": [2]},
                {"zebra": "w", "alpha": 3, "middle": [], "first": None},
            ]

        @app.post("/out_of_order_body", response_model=OutOfOrder)
        async def out_of_order_body(value: OutOfOrder) -> OutOfOrder:
            return value

        @app.post("/query")
        async def query(order: Order, limit: int) -> dict:
            return {"items": len(order.items), "limit": limit}

        @app.post("/constructed")
        async def constructed(body: ConstructedEnvelope) -> dict:
            construction_events.append("handler")
            return {"count": len(body.items)}

        @app.post("/finalized")
        async def finalized(body: FinalizedEnvelope) -> dict:
            construction_events.append("handler")
            return {"count": len(body.items)}

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
        # Keys are written in field order, matching json.dumps and FastAPI.
        self.assertEqual(
            response.content,
            json.dumps(
                Order.model_validate(coerced).model_dump(mode="json"),
                separators=(",", ":"),
            ).encode(),
        )

    def test_invalid_json_does_not_construct_models(self):
        for path in ("/constructed", "/finalized"):
            for data in (
                b'{"items":[{"value":1}]} trailing',
                b'{"items":[{"value":1},]}',
            ):
                with self.subTest(path=path, data=data):
                    construction_events.clear()
                    response = self.client.post(
                        path,
                        content=data,
                        headers={"content-type": "application/json"},
                    )
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(construction_events, [])

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

    def test_every_event_loop_uses_the_fast_path(self):
        from unittest import mock

        from bessemer import App
        from bessemer.testclient import TestClient

        app = App()

        @app.post("/echo")
        async def echo(order: Order) -> Order:
            return order

        body = {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]}
        with (
            TestClient(app, server_options={"loops": 3}) as client,
            mock.patch.object(
                Order, "model_validate", side_effect=Order.model_validate
            ) as validate,
        ):
            for _ in range(9):
                response = client.post("/echo", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"][0]["price"], 2.0)
            validate.assert_not_called()

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

    def test_raw_bodies_keep_the_order_of_checks(self):
        valid = b'{"items": []}'
        # Invalid JSON is refused before the other parameters are checked.
        for data in (
            b'{"items": [}',
            b'{"items": []} x',
            b"\xff",
            b"[" * 200 + b"]" * 200,
        ):
            with self.subTest(data=data):
                response = self.client.post(
                    "/query", content=data, headers={"content-type": "application/json"}
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"][0]["type"], "json_invalid")
        response = self.client.post(
            "/query", content=valid, headers={"content-type": "application/json"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["query", "limit"])
        response = self.client.post(
            "/query?limit=3",
            content=b' {"items": [], "items": [{"id": 1, "name": "a", "price": 2, '
            b'"tags": []}]} ',
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.json(), {"items": 1, "limit": 3})

    def test_optional_and_dict_responses(self):
        self.assertEqual(self.client.post("/optional").json(), {"none": True})
        self.assertEqual(
            self.client.post("/optional", json={"items": []}).json(), {"none": False}
        )
        body = {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]}
        response = self.client.post("/plain", json=body)
        self.assertEqual(response.json(), Item(**body["items"][0]).model_dump())

    def test_body_constraints_still_see_the_raw_json(self):
        response = self.client.post("/constrained", json={"Value": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"Value": 1, "out": "x"})
        self.assertEqual(self.client.post("/constrained", json={}).status_code, 422)

    def test_an_overridden_model_validate_still_runs(self):
        body = {"id": -1, "name": "a", "price": 2, "tags": []}
        self.assertEqual(self.client.post("/checked", json=body).status_code, 422)
        body["id"] = 1
        self.assertEqual(self.client.post("/checked", json=body).json(), {"id": 1})

    def test_an_invalid_returned_instance_still_fails_like_pydantic(self):
        body = {"items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}]}
        response = self.client.post("/changed", json=body)
        self.assertEqual(response.status_code, 500)

    def test_response_model_list_takes_the_native_path(self):
        from unittest import mock

        from pydantic import TypeAdapter

        body = [
            {"id": index, "name": "x", "price": 1.5, "tags": ["a"]}
            for index in range(3)
        ]
        with (
            mock.patch.object(
                TypeAdapter,
                "validate_python",
                side_effect=TypeAdapter.validate_python,
            ) as validate,
            mock.patch.object(
                Item,
                "model_validate",
                side_effect=Item.model_validate,
            ),
        ):
            response = self.client.get("/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [Item(**item).model_dump(mode="json") for item in body],
        )
        # Neither the response adapter nor a Pydantic instance ran: the list
        # of dicts was validated and encoded natively in one pass.
        validate.assert_not_called()

    def test_response_model_bytes_match_fastapi_key_order(self):
        """The response's bytes are what FastAPI emits: compact JSON of the
        declaration-ordered model dump (Pydantic's model_dump_json), for a
        list of dicts."""
        from unittest import mock

        from pydantic import TypeAdapter

        with mock.patch.object(
            TypeAdapter,
            "validate_python",
            side_effect=TypeAdapter.validate_python,
        ) as validate:
            response = self.client.get("/models")
            assert response.status_code == 200
            items = [
                Item(id=index, name="x", price=1.5, tags=["a"]) for index in range(3)
            ]
            # FastAPI serialises a list[Item] response through its declared
            # model: compact, declaration-ordered model JSON.
            self.assertEqual(
                response.content,
                TypeAdapter(list[Item]).dump_json(items, by_alias=True),
            )
            self.assertEqual(
                response.content,
                b"["
                + b",".join(
                    item.model_dump_json(by_alias=True).encode() for item in items
                )
                + b"]",
            )
        validate.assert_not_called()

    def test_out_of_order_bytes_match_fastapi_for_lists_and_bodies(self):
        """The same for a model whose declaration order is not alphabetical
        and whose last field carries a serialization alias: list items as
        dicts, and a body model returned by the handler."""
        from pydantic import TypeAdapter

        values = [
            {"zebra": "z", "alpha": 2, "middle": [2]},
            {"zebra": "w", "alpha": 3, "middle": [], "first": None},
        ]
        adapter = TypeAdapter(list[OutOfOrder])
        validated = adapter.validate_python(values)
        expected = adapter.dump_json(validated, by_alias=True)
        response = self.client.get("/out_of_order")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, expected)
        # The declaration order comes through (not byte-sorted keys).
        self.assertTrue(
            response.content.index(b'"zebra"')
            < response.content.index(b'"alpha"')
            < response.content.index(b'"middle"')
        )

        orders = [
            ("s", 2, [3], {"a": Item(id=1, name="x", price=1.5, tags=["a"])}),
            ("t", 3, [], None),
        ]
        body = [
            {
                "zebra": z,
                "alpha": a,
                "middle": m,
                "first": (f if f is None else vars(f["a"])),
            }
            for z, a, m, f in orders
        ]
        body_adapter = TypeAdapter(OutOfOrder)
        response = self.client.post("/out_of_order_body", json=body[0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content,
            body_adapter.dump_json(
                body_adapter.validate_python(body[0]), by_alias=True
            ),
        )
        # Alias key, declaration-position, and default-filling all match.
        self.assertTrue(
            response.content.index(b'"FIRST"') > response.content.index(b'"middle"')
        )
        self.assertTrue(
            response.content.startswith(b'{"zebra":"s","alpha":2,"middle":[3],"FIRST":')
        )

    def test_response_model_list_of_instances_and_a_body_route(self):
        response = self.client.post(
            "/models_post",
            json={"items": [{"id": 2, "name": "y", "price": 2, "tags": []}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [Item(id=2, name="y", price=2.0, tags=[]).model_dump(mode="json")],
        )

    def test_wrapped_response_routes_match_fastapi_bytes(self):
        """Optional, nested-list and dict response_model routes emit exactly
        the bytes of the declared model's dump_json (FastAPI's JSON), through
        the native path and through the middleware (Python) path."""
        from bessemer import App
        from bessemer.testclient import TestClient
        from pydantic import TypeAdapter

        order = {
            "items": [{"id": 1, "name": "a", "price": 2, "tags": ["x"]}],
            "primary": None,
        }
        nested = [
            [
                {"id": 1, "name": "a", "price": 2, "tags": ["x"]},
                {"id": 2, "name": "b", "price": 1.5, "tags": []},
            ],
            [],
        ]
        shaped = {
            "zebra": {"zebra": "s", "alpha": 1, "middle": [1]},
            "alpha": {"zebra": "t", "alpha": 2},
        }
        routes = (
            ("/optional_model", Order | None, order),
            ("/nested_lists", list[list[Item]], nested),
            ("/model_dict", dict[str, OutOfOrder], shaped),
        )

        def add_routes(app, with_middleware):
            for path, annotation, value in routes:

                def route(path=path, annotation=annotation, value=value):
                    return value

                if with_middleware:

                    @app.get(path.lstrip("/"), response_model=annotation)
                    async def handler():
                        return route()
                else:
                    # The main app already declares these with query inputs.
                    return

        # The declared app's routes (without inputs) exercise the native
        # invoker; a plain None result exercises the optional root.
        absent = self.client.get("/optional_model?absent=1")
        self.assertEqual(absent.status_code, 200)
        self.assertEqual(absent.content, b"null")
        for path, annotation, value in routes:
            expected = TypeAdapter(annotation).dump_json(
                TypeAdapter(annotation).validate_python(value), by_alias=True
            )
            fast = self.client.get(path)
            self.assertEqual(fast.status_code, 200, path)
            self.assertEqual(fast.content, expected, path)

        # Rebuilt behind a middleware, the same bytes come out of the
        # worker's Python response path.
        middleware = App()

        @middleware.middleware("http")
        async def passthrough(request, call_next):
            return await call_next(request)

        for path, annotation, value in routes:

            def make_handler(annotation=annotation, value=value):
                async def handler(absent: bool = False):
                    if absent:
                        return None
                    return value

                return handler

            middleware.get(path, response_model=annotation)(
                make_handler(annotation, value)
            )
        with TestClient(middleware) as client:
            for path, annotation, value in routes:
                expected = TypeAdapter(annotation).dump_json(
                    TypeAdapter(annotation).validate_python(value), by_alias=True
                )
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.content, expected, path)
                if "optional" in path:
                    absent = client.get(path + "?absent=1")
                    self.assertEqual(absent.status_code, 200)
                    self.assertEqual(absent.content, b"null")

    def test_response_model_list_errors_still_match_pydantic(self):
        from bessemer import App
        from bessemer.testclient import TestClient

        app = App()

        @app.middleware("http")
        async def passthrough(request, call_next):
            return await call_next(request)

        @app.get("/models", response_model=list[Item])
        async def models():
            return [{"id": "x", "name": "a", "price": 2, "tags": []}]

        @app.get("/broken_mw", response_model=list[Item])
        async def broken_mw():
            return [{"id": "x", "name": "a", "price": 2, "tags": []}]

        with TestClient(app) as client:
            # Invalid items of a response_model list fail the response exactly
            # as the Pydantic pipeline does, fast path or middleware path.
            for route in ("/models", "/broken_mw"):
                with self.subTest(route=route):
                    self.assertEqual(client.get(route).status_code, 500)


if __name__ == "__main__":
    unittest.main()

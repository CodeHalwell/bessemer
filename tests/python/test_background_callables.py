"""Task classification follows code changes and inspect's wrapper semantics."""

import functools
import inspect
import unittest

from bessemer.background import BackgroundTask


class BackgroundCallableTests(unittest.TestCase):
    def test_code_replacement_changes_classification_without_a_stale_cache(self):
        def call():
            return None

        async def asynchronous():
            return None

        def generator():
            yield None

        async def async_generator():
            yield None

        self.assertEqual(BackgroundTask(call).kind, "sync")
        call.__code__ = asynchronous.__code__
        self.assertEqual(BackgroundTask(call).kind, "async")
        for source in (generator, async_generator):
            call.__code__ = source.__code__
            with self.assertRaisesRegex(TypeError, "cannot be generator"):
                BackgroundTask(call)

    def test_marked_partial_and_callable_tasks_keep_inspect_semantics(self):
        @inspect.markcoroutinefunction
        def marked():
            return None

        async def asynchronous(value=None):
            return value

        class Callable:
            async def __call__(self):
                return None

        for call in (marked, functools.partial(asynchronous, 1), Callable()):
            with self.subTest(call=call):
                self.assertEqual(BackgroundTask(call).kind, "async")

        def generator(value=None):
            yield value

        with self.assertRaisesRegex(TypeError, "cannot be generator"):
            BackgroundTask(functools.partial(generator, 1))

    def test_function_attribute_mapping_is_not_interpreted_as_user_boolean(self):
        class Attributes(dict):
            def __bool__(self):
                raise AssertionError("classification must not call this")

        def call():
            return None

        call.__dict__ = Attributes()
        self.assertEqual(BackgroundTask(call).kind, "sync")

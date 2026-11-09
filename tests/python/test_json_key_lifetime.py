"""Object keys parsed from request JSON must not outlive the parsed value.

An interned string is immortal on CPython 3.12 and on free-threaded builds,
so interning keys chosen by a client grows the process without bound.
"""

import gc
import importlib.util
import sys
import tracemalloc
import unittest
import uuid

NATIVE = importlib.util.find_spec("bessemer._native") is not None


@unittest.skipUnless(NATIVE, "build the native extension first")
class JsonKeyLifetimeTests(unittest.TestCase):
    def parse(self, document: bytes):
        from bessemer._native import _parse_json

        return _parse_json(document)

    def fresh_key(self) -> str:
        return "k" + uuid.uuid4().hex

    def test_a_parsed_key_is_an_ordinary_mortal_string(self):
        name = self.fresh_key()
        parsed = self.parse(b'{"%s": 1}' % name.encode())
        (key,) = parsed
        self.assertEqual(key, name)
        # An immortal object reports a reference count in the billions.
        self.assertLess(sys.getrefcount(key), 1_000_000)
        is_interned = getattr(sys, "_is_interned", None)
        if is_interned is not None:
            self.assertFalse(is_interned(key))

    def test_keys_longer_than_the_cache_are_mortal_too(self):
        name = self.fresh_key() * 4
        (key,) = self.parse(b'{"%s": 1}' % name.encode())
        self.assertLess(sys.getrefcount(key), 1_000_000)

    def test_repeated_keys_share_one_object_within_a_document(self):
        name = self.fresh_key()
        encoded = name.encode()
        first, second = self.parse(b'[{"%s": 1}, {"%s": 2}]' % (encoded, encoded))
        (left,) = first
        (right,) = second
        self.assertIs(left, right)

    def test_fresh_keys_do_not_accumulate(self):
        # The key cache keeps a bounded number of recent keys; every other key
        # must be freed with the parsed value. 40,000 immortal keys would hold
        # several megabytes for the life of the process.
        prefix = self.fresh_key().encode()

        def parse_batch(batch: int) -> None:
            body = (
                b"{"
                + b",".join(
                    b'"%s%05d%03d": 0' % (prefix, index, batch) for index in range(2000)
                )
                + b"}"
            )
            self.assertEqual(len(self.parse(body)), 2000)

        parse_batch(0)  # Fill the key cache before measuring.
        gc.collect()
        tracemalloc.start()
        try:
            before, _ = tracemalloc.get_traced_memory()
            for batch in range(1, 21):
                parse_batch(batch)
            gc.collect()
            after, _ = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(after - before, 1_048_576)


if __name__ == "__main__":
    unittest.main()

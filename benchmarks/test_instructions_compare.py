"""The performance gate must never zero another profiler's counters."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "instructions_compare", Path(__file__).parent / "profile/instructions_compare.py"
)
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


class InstructionsCompareTests(unittest.TestCase):
    def test_measure_uses_one_private_prefix_for_worker_zero_and_dump(self):
        commands = []

        def control(*args):
            commands.append(args)
            prefix = args[0].removeprefix("--vgdb-prefix=")
            if args[-1] == "-d":
                (Path(prefix).parent / "callgrind.out.123.1").write_text(
                    "summary: 123000\n"
                )

        server = Mock(pid=123)
        with (
            patch.object(compare.subprocess, "Popen", return_value=server) as popen,
            patch.object(compare, "wait_ready"),
            patch.object(compare, "drive"),
            patch.object(compare, "callgrind_control", side_effect=control),
            patch.object(compare.os, "killpg"),
            patch.object(compare.time, "sleep"),
        ):
            self.assertEqual(
                compare.measure("python", "json", "bessemer", 100, 10), 1230
            )
        self.assertEqual([args[-1] for args in commands], ["-z", "-d"])
        self.assertEqual(commands[0][0], commands[1][0])
        self.assertTrue(commands[0][0].startswith("--vgdb-prefix="))
        self.assertIn(commands[0][0], popen.call_args.args[0])

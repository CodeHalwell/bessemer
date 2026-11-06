"""Applies BESSEMER_EVENT_LOOP to the suite (see event_loop_env) and checks it.

Discovery imports this module before running any test, so the override is in
place for every module. It does nothing when the variable is unset.
"""

import os
import unittest

import event_loop_env
from bessemer import App, config
from bessemer.worker import Worker

event_loop_env.apply()


class ForcedEventLoopTests(unittest.TestCase):
    def test_the_forced_loop_is_the_default_everywhere(self):
        value = event_loop_env.forced()
        if value is None:
            self.skipTest("BESSEMER_EVENT_LOOP is not set to a loop")
        self.assertEqual(config.DEFAULTS["event_loop"], value)
        self.assertEqual(config.SERVER_DEFAULTS["event_loop"], value)
        for function in (App.run, App._run, Worker.__init__):
            self.assertEqual(function.__kwdefaults__["event_loop"], value)

    def test_without_the_variable_the_shipped_default_applies(self):
        if "BESSEMER_EVENT_LOOP" in os.environ:
            self.skipTest("BESSEMER_EVENT_LOOP is set")
        self.assertEqual(config.DEFAULTS["event_loop"], "auto")
        self.assertEqual(App._run.__kwdefaults__["event_loop"], "auto")

    def test_shipped_default_restores_the_forced_loop(self):
        before = config.SERVER_DEFAULTS["event_loop"]
        with event_loop_env.shipped_default():
            self.assertEqual(config.SERVER_DEFAULTS["event_loop"], "auto")
            self.assertEqual(App._run.__kwdefaults__["event_loop"], "auto")
        self.assertEqual(config.SERVER_DEFAULTS["event_loop"], before)


if __name__ == "__main__":
    unittest.main()

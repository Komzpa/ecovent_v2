"""Regression tests for Home Assistant sensor helper behavior."""

import ast
import re
import unittest
import types

from ecovent_test_helpers import COMPONENT_PATH  # noqa: F401  Ensures sys.path setup.
from sensor_helpers import enum_options_with_value


SENSOR_PATH = COMPONENT_PATH / "sensor.py"


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _class_method(tree, name):
    sensor_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "VentoSensor"
    )
    return next(
        node
        for node in sensor_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


class SensorEnumOptionsTest(unittest.TestCase):
    def test_keeps_known_enum_options_unchanged(self):
        self.assertEqual(
            enum_options_with_value(["off", "on"], "on"),
            ["off", "on"],
        )

    def test_adds_unknown_current_enum_value(self):
        self.assertEqual(
            enum_options_with_value(["off", "on"], "Unknown beeper 3"),
            ["off", "on", "Unknown beeper 3"],
        )

    def test_preserves_non_enum_without_options(self):
        self.assertIsNone(enum_options_with_value(None, "Unknown beeper 3"))

    def test_filter_remaining_uses_the_decoded_filter_countdown(self):
        tree = ast.parse(SENSOR_PATH.read_text())
        nodes = [
            _function(tree, "_parse_hours"),
            _function(tree, "_parse_days"),
            _class_method(tree, "filter_timer_countdown"),
            _class_method(tree, "filter_remaining"),
        ]
        namespace = {"re": re}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                str(SENSOR_PATH),
                "exec",
            ),
            namespace,
        )
        sensor_instance = types.SimpleNamespace(
            _fan=types.SimpleNamespace(
                filter_timer_countdown="90d 0h 0m ",
                filter_timer_setpoint="180 d",
            )
        )
        sensor_instance.filter_timer_countdown = types.MethodType(
            namespace["filter_timer_countdown"], sensor_instance
        )

        self.assertEqual(sensor_instance.filter_timer_countdown(), 2160.0)
        self.assertEqual(namespace["filter_remaining"](sensor_instance), 50)


if __name__ == "__main__":
    unittest.main()

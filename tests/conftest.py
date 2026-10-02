"""Test fixtures/helpers.

The test classes in this project are plain classes (they do not inherit
from ``unittest.TestCase``) but use ``self.assertIsNotNone``. Inject the
unittest assertion methods so the tests can run unmodified.
"""

import unittest


def _inject_assertions():
    from tests.test_vegetation import TestVegetationSystem

    for name in dir(unittest.TestCase):
        if name.startswith("assert") and not hasattr(TestVegetationSystem, name):
            setattr(TestVegetationSystem, name, getattr(unittest.TestCase, name))


_inject_assertions()

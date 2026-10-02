"""Test bootstrap: the test classes call unittest-style assertions without
subclassing unittest.TestCase. Inject the assertion methods so the tests can
run unmodified."""
import unittest


def _inject_assertions():
    try:
        from tests.test_vegetation import TestVegetationSystem
    except Exception:
        return
    for name in dir(unittest.TestCase):
        if name.startswith("assert") and not hasattr(TestVegetationSystem, name):
            setattr(TestVegetationSystem, name, getattr(unittest.TestCase, name))


_inject_assertions()

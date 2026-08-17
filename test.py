"""Convenience entry point for the pure-Python test suite."""

import unittest


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover('tests')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

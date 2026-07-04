#!/usr/bin/env python3
"""Run every no-hardware test suite. Exit non-zero if any assertion fails.

    python tests/run_all.py
"""
import sys, pathlib, importlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))

for mod in ('test_protocol', 'test_writeops', 'test_functions', 'test_layout', 'test_decode'):
    print(f"\n===== {mod} =====")
    m = importlib.import_module(mod)
    if hasattr(m, 'main'):
        m.main()
    else:
        for name in dir(m):
            if name.startswith('test_'):
                getattr(m, name)()
print("\nAll suites passed.")

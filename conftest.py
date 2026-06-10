"""
conftest.py — Root pytest configuration
Adds src/ to sys.path so that `from sentinel.xxx import ...` works
regardless of which directory pytest is invoked from.
"""
import sys
import os

# Insert src/ at the front of the path so sentinel package is always found
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

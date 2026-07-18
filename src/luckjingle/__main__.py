"""Enable `python -m luckjingle` invocation.

Equivalent to the `luckjingle-print` console-script entry point defined in
pyproject.toml.
"""
import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

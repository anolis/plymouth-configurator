#!/usr/bin/env python3
"""Run Plymouth Configurator from the source tree."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plymouth_configurator.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

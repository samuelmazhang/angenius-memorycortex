"""pytest auto-loaded conftest for the integration tests directory.

Pytest does not add the test's own directory to ``sys.path`` by default
(this is a long-standing pytest-vs-unittest behavioral difference --
``python -m unittest`` would, but pytest won't). Our integration tests
import the shared helpers via ``from _shared import ...`` so we have to
splice the directory into ``sys.path`` ourselves.

This file is picked up automatically by pytest before test collection.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
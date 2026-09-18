#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys


APP_DIRECTORY = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(APP_DIRECTORY / ".matplotlib_cache"))

from ppg_collector.app import run  # noqa: E402


if __name__ == "__main__":
    run(smoke_test="--smoke-test" in sys.argv)

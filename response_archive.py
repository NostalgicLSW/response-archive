#!/usr/bin/env python3
"""Repository entry point; installed skills carry the same standalone tool."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills/response-archive/scripts"))
from archive import main

if __name__ == "__main__":
    raise SystemExit(main())

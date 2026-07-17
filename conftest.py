"""Pytest config: make `app` importable and enable asyncio mode."""
import os
import sys
from pathlib import Path

# Add project root to sys.path so `import app...` works from anywhere.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    # Silence unnecessary noise; let tests run fast.
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

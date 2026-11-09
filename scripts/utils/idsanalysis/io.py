"""CSV loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


def load_csv(path: Path) -> Optional[pd.DataFrame]:
    """Load a plain (Phase-1) CSV; None if missing or empty."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    return pd.read_csv(path)


def load_csv_comment(path: Path) -> Optional[pd.DataFrame]:
    """Load a Phase-2 CSV that prefixes the header with a `# schema_version=…` comment line."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    return pd.read_csv(path, comment="#")

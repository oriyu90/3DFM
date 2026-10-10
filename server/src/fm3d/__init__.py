"""3DFM local inference server package."""
from .paths import DataDirs, resolve_data_dir

__all__ = ["DataDirs", "resolve_data_dir"]
# Single source of truth for the release version. main.py (FastAPI +
# /health) and scripts/build-*.sh read this; bump here on release.
__version__ = "0.3.1"

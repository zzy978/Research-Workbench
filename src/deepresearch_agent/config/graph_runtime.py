"""Native dependency boundary used only by legacy graph implementations."""
import sys

# pandas can load PyArrow's bundled C++ runtime before Torch's c10.dll on
# Windows. Initialize Torch first here; Hybrid/Web keep their lazy model load.
if sys.platform == 'win32':
    import torch  # noqa: F401

import pandas

__all__ = ['pandas']

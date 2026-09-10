"""Exercise real native-library initialization in a fresh interpreter."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows DLL initialization regression')
def test_graph_dataframe_and_torch_can_coexist():
    env = {**os.environ, 'PYTHONPATH': str(Path('src').resolve())}
    process = subprocess.run([sys.executable, '-c',
        'from deepresearch_agent.config.graph_runtime import pandas as pd; '
        'import torch; assert pd.DataFrame({"x":[1]}).iloc[0,0] == 1; '
        'assert torch.tensor([0.]).sigmoid().item() == 0.5'],
        env=env, capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr

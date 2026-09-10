"""Default imports must work without the legacy graph and vector stack."""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_default_entrypoints_and_plain_cache_without_graph_dependencies(tmp_path):
    script = '''
import importlib.abc
import sys

blocked = {"faiss", "graphdatascience", "langchain_neo4j", "hanlp", "schedule", "shutup"}
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"Optional dependency blocked: {fullname}", name=fullname)
sys.meta_path.insert(0, BlockOptional())

import backend.app.main
import build_rag_index
import deepresearch_agent.models.get_models
from deepresearch_agent.pipelines.ingestion.file_reader import FileReader
from deepresearch_agent.pipelines.ingestion import FileReader as PublicFileReader
from deepresearch_agent.cache_manager import CacheManager

assert PublicFileReader is FileReader
cache = CacheManager(memory_only=True, enable_vector_similarity=False)
cache.set("question", "answer")
assert cache.get("question") == "answer"
assert not blocked.intersection(sys.modules)

# Explicitly opting into the legacy vector cache still requests FAISS.
try:
    CacheManager(memory_only=True, enable_vector_similarity=True)
except ModuleNotFoundError as exc:
    assert exc.name == "faiss"
else:
    raise AssertionError("Vector cache must not silently ignore missing FAISS")

# The historical public matcher export remains lazy and available on request.
try:
    from deepresearch_agent.cache_manager import VectorSimilarityMatcher
except ModuleNotFoundError as exc:
    assert exc.name == "faiss"
else:
    raise AssertionError("The public matcher must request its optional dependency")
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
             "PRIVATE_RETRIEVAL_BACKEND": "hybrid"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

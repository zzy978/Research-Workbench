"""File reading is shared; legacy graph chunking is loaded only on demand."""

from importlib import import_module


def __getattr__(name):
    modules = {
        "DocumentProcessor": ".document_processor",
        "FileReader": ".file_reader",
        "ChineseTextChunker": ".text_chunker",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(modules[name], __name__), name)

__all__ = [
    'DocumentProcessor',
    'FileReader',
    'ChineseTextChunker'
]

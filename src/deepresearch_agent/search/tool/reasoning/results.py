"""不依赖检索后端的多轮研究结果合并。"""

from copy import deepcopy
from typing import Dict


def merge_search_results(result1: Dict, result2: Dict) -> Dict:
    """按证据和文档 ID 去重，保留两轮元数据且不修改输入。"""
    result = deepcopy(result1)
    for key, id_field in (("chunks", "chunk_id"), ("doc_aggs", "doc_id")):
        items = result.setdefault(key, [])
        existing_ids = {item.get(id_field) for item in items if item.get(id_field)}
        for item in result2.get(key, []):
            item_id = item.get(id_field)
            if item_id:
                if item_id in existing_ids:
                    continue
                existing_ids.add(item_id)
            elif key == "chunks":
                if any(old.get("text") == item.get("text", "") for old in items):
                    continue
            elif item in items:
                continue
            items.append(deepcopy(item))

    for key, value in result2.items():
        if key in ("chunks", "doc_aggs"):
            continue
        if key not in result:
            result[key] = deepcopy(value)
        elif isinstance(result[key], list) and isinstance(value, list):
            result[key].extend(deepcopy(item) for item in value if item not in result[key])
    return result

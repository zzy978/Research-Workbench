"""按需复用旧图工具的关键词提取，避免研究工具构造时连接 Neo4j。"""


class LegacyGraphKeywords:
    def __init__(self):
        self._tool = None

    def extract_keywords(self, query):
        if self._tool is None:
            from deepresearch_agent.search.tool.hybrid_tool import HybridSearchTool
            self._tool = HybridSearchTool()
        return self._tool.extract_keywords(query)

    def close(self):
        if self._tool is not None:
            self._tool.close()

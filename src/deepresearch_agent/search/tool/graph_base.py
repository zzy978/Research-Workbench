"""仅供显式 GraphRAG 工具使用的图数据库搜索基础类。"""

from typing import Any, Dict, List

from deepresearch_agent.config.settings import BASE_SEARCH_CONFIG
from deepresearch_agent.search.tool.base import BaseSearchTool


class GraphSearchTool(BaseSearchTool):
    """拥有 Neo4j 连接和 Cypher/图向量查询能力。"""

    def __init__(self, cache_dir: str = "./cache/search", *, enable_vector_cache: bool | None = None):
        super().__init__(cache_dir=cache_dir, enable_vector_cache=enable_vector_cache)
        self.default_vector_limit = BASE_SEARCH_CONFIG["vector_limit"]
        self.default_text_limit = BASE_SEARCH_CONFIG["text_limit"]
        self._setup_neo4j()

    def _setup_neo4j(self):
        """设置Neo4j连接"""
        from deepresearch_agent.config.neo4jdb import get_db_manager
        # 获取数据库连接管理器
        db_manager = get_db_manager()
        
        # 获取图数据库实例
        self.graph = db_manager.get_graph()
        
        # 获取驱动（用于直接执行查询）
        self.driver = db_manager.get_driver()
    
    def db_query(self, cypher: str, params: Dict[str, Any] | None = None):
        """
        执行Cypher查询
        
        参数:
            cypher: Cypher查询语句
            params: 查询参数
            
        返回:
            查询结果
        """
        # 使用连接管理器执行查询
        from deepresearch_agent.config.neo4jdb import get_db_manager
        return get_db_manager().execute_query(cypher, params or {})
        
    def vector_search(self, query: str, limit: int = None) -> List[str]:
        """
        基于向量相似度的搜索方法
        
        参数:
            query: 搜索查询
            limit: 最大返回结果数
            
        返回:
            List[str]: 匹配实体ID列表
        """
        try:
            limit = limit or self.default_vector_limit
            # 生成查询的嵌入向量
            query_embedding = self.embeddings.embed_query(query)
            
            # 构建Neo4j向量搜索查询
            cypher = """
            CALL db.index.vector.queryNodes('vector', $limit, $embedding)
            YIELD node, score
            RETURN node.id AS id, score
            ORDER BY score DESC
            """
            
            # 执行搜索
            results = self.db_query(cypher, {
                "embedding": query_embedding,
                "limit": limit
            })
            
            # 提取实体ID
            if not results.empty:
                return results['id'].tolist()
            else:
                return []
                
        except Exception as e:
            print(f"向量搜索失败: {e}")
            # 如果向量搜索失败，尝试使用文本搜索作为备用
            return self.text_search(query, limit)
    
    def text_search(self, query: str, limit: int = None) -> List[str]:
        """
        基于文本匹配的搜索方法（作为向量搜索的备选）
        
        参数:
            query: 搜索查询
            limit: 最大返回结果数
            
        返回:
            List[str]: 匹配实体ID列表
        """
        try:
            limit = limit or self.default_text_limit
            # 构建全文搜索查询
            cypher = """
            MATCH (e:__Entity__)
            WHERE e.id CONTAINS $query OR e.description CONTAINS $query
            RETURN e.id AS id
            LIMIT $limit
            """
            
            results = self.db_query(cypher, {
                "query": query,
                "limit": limit
            })
            
            if not results.empty:
                return results['id'].tolist()
            else:
                return []
                
        except Exception as e:
            print(f"文本搜索失败: {e}")
            return []
            
    def close(self):
        """关闭资源连接"""
        # 关闭Neo4j连接
        if hasattr(self, 'graph'):
            # 如果Neo4jGraph有close方法，调用它
            if hasattr(self.graph, 'close'):
                self.graph.close()
    

"""Task vocabulary and the single boundary for legacy retrieval aliases."""
from dataclasses import dataclass

from deepresearch_agent.harness.contracts import SourceMode


@dataclass(frozen=True)
class TaskCapabilities:
    source_mode: str
    backend: str
    tasks: tuple[tuple[str, str], ...]

    def prompt_description(self) -> str:
        return '\n'.join(f'- {name}: {description}' for name, description in self.tasks)

    def validate(self, task_type: str) -> str:
        if task_type not in dict(self.tasks):
            raise ValueError(f'{self.backend} 不支持任务 {task_type}；请按当前可用能力重新规划')
        return task_type


def task_capabilities(source_mode, *, provider=None) -> TaskCapabilities:
    mode = SourceMode(source_mode)
    if provider is not None and SourceMode(provider.mode) != mode:
        raise ValueError('规划能力与 Provider 信息源不一致')
    if mode == SourceMode.WEB:
        backend = 'web'
        retrieval = [('web_search', '检索公开网页证据')]
    else:
        if provider is None:
            from deepresearch_agent.config.settings import PRIVATE_RETRIEVAL_BACKEND
            graph = PRIVATE_RETRIEVAL_BACKEND == 'graphrag'
        else:
            from deepresearch_agent.retrieval.base import provider_supports_graph
            graph = provider_supports_graph(provider)
        backend = 'graphrag' if graph else 'hybrid'
        retrieval = ([
            ('local_search', '检索图谱实体及局部关系'),
            ('global_search', '检索图谱社区摘要'),
            ('hybrid_search', '结合图谱结构与向量语义检索'),
            ('naive_search', '检索图谱中保存的文本片段'),
            ('chain_exploration', '沿图谱实体关系路径探索'),
        ] if graph else [('hybrid_search', '对私有文档进行向量、BM25 混合检索与重排')])
    retrieval += [('deep_research', '通过当前信息源的多轮检索调查复杂子问题')]
    if backend == 'graphrag':
        retrieval += [('deeper_research', '使用图谱关系探索增强多轮研究')]
    return TaskCapabilities(mode.value, backend, tuple(retrieval + [('reflection', '校验证据和答案')]))


def adapt_legacy_task(task_type: str, capabilities: TaskCapabilities) -> str:
    """Read historical task labels without duplicating translation across callers.

    New model plans must use validate(), never this lossy compatibility adapter.
    """
    graph_searches = {'local_search', 'global_search', 'hybrid_search', 'naive_search', 'chain_exploration'}
    if capabilities.backend == 'web':
        if task_type in graph_searches:
            return 'web_search'
        if task_type == 'deeper_research':
            return 'deep_research'
    elif capabilities.backend == 'hybrid':
        if task_type in graph_searches or task_type == 'web_search':
            return 'hybrid_search'
        if task_type == 'deeper_research':
            return 'deep_research'
    elif task_type == 'web_search':
        return 'hybrid_search'
    return task_type

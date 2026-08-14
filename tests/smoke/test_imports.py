def test_existing_and_new_modules_import():
    from graphrag_agent.agents.deep_research_agent import DeepResearchAgent  # noqa: F401
    from graphrag_agent.agents.multi_agent.orchestrator import MultiAgentOrchestrator  # noqa: F401
    from graphrag_agent.harness import RunStatus, SourceMode  # noqa: F401
    from graphrag_agent.persistence import ArtifactStore, Database  # noqa: F401

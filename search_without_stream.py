import argparse
import time
from datetime import datetime

from graphrag_agent.agents.deep_research_agent import DeepResearchAgent
from graphrag_agent.agents.fusion_agent import FusionGraphRAGAgent


AGENT_FACTORIES = {
    "fusion": lambda: FusionGraphRAGAgent(),
    "deep_research": lambda: DeepResearchAgent(use_deeper_tool=True),
}


def run_query(agent_name: str, query: str, thread_id: str) -> None:
    agent = AGENT_FACTORIES[agent_name]()
    try:
        start_time = time.time()
        answer = agent.ask(query, thread_id=thread_id)
        elapsed = time.time() - start_time
        print(f"\n[{agent_name}] finished in {elapsed:.2f}s")
        print(answer)
    finally:
        if hasattr(agent, "close"):
            agent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an existing GraphRAG agent smoke query")
    parser.add_argument("query", nargs="?", default="急性脑⾎管病吃什么药？写一份研究报告")
    parser.add_argument("--agent", choices=sorted(AGENT_FACTORIES), default="deep_research")
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()
    agent_name = args.agent
    query = args.query
    thread_id = args.thread_id or f"{agent_name}_{int(time.time())}"
    print(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run_query(agent_name, query, thread_id)


if __name__ == "__main__":
    main()

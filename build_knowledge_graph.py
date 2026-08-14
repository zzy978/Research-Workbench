from graphrag_agent.integrations.build.main import KnowledgeGraphProcessor


def main() -> None:
    processor = KnowledgeGraphProcessor()
    processor.process_all()


if __name__ == "__main__":
    main()

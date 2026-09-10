"""Local server configuration with a bounded HTTP shutdown phase."""

import uvicorn


def create_config(*, app="backend.app.main:app", host="127.0.0.1", port=8000):
    # Uvicorn otherwise waits forever before sending lifespan.shutdown, including
    # when asyncio.Server.wait_closed() stalls after connections disappear.
    return uvicorn.Config(
        app, host=host, port=port, workers=1, timeout_graceful_shutdown=5,
    )


def main():
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="启动本地研究后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    try:
        uvicorn.Server(create_config(host=args.host, port=args.port)).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

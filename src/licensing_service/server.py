"""服务启动入口：python -m licensing_service.server --db data/license.db --port 8080"""
from __future__ import annotations

import argparse

from .app import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="职业教育资源授权后端")
    parser.add_argument("--db", default="data/license.db")
    parser.add_argument("--artifacts", default="data/artifacts")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server, _ = create_server(args.db, args.artifacts, args.host, args.port)
    print(f"授权后端已启动：http://{args.host}:{args.port}（库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

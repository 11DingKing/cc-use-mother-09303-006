"""启动入口：python -m licensing [--host H] [--port P] [--dev-auth]

环境变量：
- LICENSE_DB：SQLite 路径（默认 ./data/licensing.sqlite3）
- LICENSE_ARTIFACT_DIR：制品目录（默认 ./data/artifacts）
- LICENSE_TOKENS_FILE：Bearer 令牌映射 JSON（生产鉴权）
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .api import AuthContext, create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="职业教育资源授权后端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.environ.get("LICENSE_DB", "data/licensing.sqlite3"))
    parser.add_argument("--artifact-dir",
                        default=os.environ.get("LICENSE_ARTIFACT_DIR", "data/artifacts"))
    parser.add_argument("--tokens-file", default=os.environ.get("LICENSE_TOKENS_FILE"))
    parser.add_argument("--dev-auth", action="store_true",
                        help="开发模式：用 X-Subject/X-Org/X-Roles 头鉴权（勿用于生产）")
    args = parser.parse_args()

    tokens: dict[str, dict] = {}
    if args.tokens_file:
        tokens = json.loads(Path(args.tokens_file).read_text(encoding="utf-8"))
    if not tokens and not args.dev_auth:
        raise SystemExit("必须提供 --tokens-file 或显式启用 --dev-auth")

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    server = create_server(
        args.host, args.port, args.db, args.artifact_dir,
        AuthContext(tokens=tokens, dev_auth=args.dev_auth),
    )
    print(f"licensing backend listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

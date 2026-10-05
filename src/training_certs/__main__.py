"""命令行入口：python -m training_certs [--db PATH] [--host H] [--port P]"""
from __future__ import annotations

import argparse

from .http_api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="培训补考证书管理服务端")
    parser.add_argument("--db", default="training_certs.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    httpd = serve(args.db, args.host, args.port)
    print(f"培训补考证书管理服务监听中：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

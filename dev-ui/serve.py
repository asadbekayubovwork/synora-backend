#!/usr/bin/env python3
"""Dev UI uchun oddiy statik server.

    python dev-ui/serve.py [port]      # default 3000

Port 3000 ataylab: `.env.example` dagi `CORS_ORIGINS` aynan
`http://localhost:3000,http://127.0.0.1:3000` ni ro'yxatlaydi, boshqa portdan
ochsangiz brauzer har bir so'rovni CORS'da to'xtatadi.
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        # UI'ni tahrirlab, brauzerni yangilash kifoya bo'lsin.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args: object) -> None:  # tinchroq log
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    handler = partial(Handler, directory=str(ROOT))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Dev UI: http://localhost:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()

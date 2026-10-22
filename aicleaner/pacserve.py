"""Route B, safeguard 2 (spec 2026-10-21-desktop-chat-route-b.md, section 6): the PAC file served by a process of
its own.

While route B is on, the Windows proxy setting points at a PAC file. Served by aicleaner itself, an aicleaner that
has stopped would take the PAC with it, and Chromium then goes DIRECT - claude.ai unfiltered, and nothing says so.
This tiny server (standard library only, no filtering, nothing else) serves the PAC aicleaner wrote to its state
folder, so with aicleaner down the PAC still sends claude.ai to aicleaner's forward proxy with no DIRECT fallback:
Desktop's chat shows a connection error instead of working unfiltered. Only if both stop does it fail open.

    python -m aicleaner.pacserve --file <state>/claudeai.pac --port 8789

Run in the foreground of its own logon task (claudeai_tasks), so Task Scheduler restarts it if it ends.
"""
from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PATH = "/claudeai.pac"
MAX_BYTES = 1024 * 1024


def handler_for(pac_file: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "aicleaner-pac"

        def do_GET(self) -> None:  # noqa: N802 - the http.server name
            if self.path.split("?", 1)[0] != PATH:
                self.send_error(404)
                return
            try:
                body = pac_file.read_bytes()[:MAX_BYTES]
            except OSError:
                self.send_error(404, "aicleaner has not written its PAC file")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - quiet: WinINET asks often
            pass

    return Handler


def serve(pac_file: Path, port: int) -> ThreadingHTTPServer:
    """The server, bound to this machine only; the caller runs `serve_forever`."""
    return ThreadingHTTPServer(("127.0.0.1", port), handler_for(Path(pac_file)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicleaner.pacserve")
    parser.add_argument("--file", required=True, help="the PAC file aicleaner writes")
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args(argv)
    try:
        server = serve(Path(args.file), args.port)
    except OSError as exc:
        print(f"aicleaner PAC service: port {args.port} is in use ({exc})", file=sys.stderr)
        return 2
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

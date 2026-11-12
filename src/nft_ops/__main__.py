"""Start the local dashboard: uv run nft-ops"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .server import AppState, create_app


def main() -> int:
    parser = argparse.ArgumentParser(prog="nft-ops", description="Local NFT ops dashboard")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data", type=Path, default=Path("data"),
                        help="folder for the encrypted vault and database (default: ./data)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--restore", type=Path, metavar="BACKUP.zip",
                        help="restore a backup into the data folder (current data is moved aside)")
    parser.add_argument("--version", action="version", version=f"nft-ops {__version__}")
    args = parser.parse_args()
    if args.restore:
        from .backup import BackupError, restore
        try:
            moved = restore(args.restore, args.data.resolve())
        except BackupError as error:
            print(f"\n  Restore failed: {error}\n  Nothing was changed.\n")
            return 1
        print(f"\n  Restored {args.restore} into {args.data.resolve()}.")
        if moved:
            print(f"  Your previous data was moved to {moved} (delete it once you are happy).")
        print("  Start NFT Ops and unlock with the password the backup was made with.\n")
        return 0

    state = AppState(args.data.resolve())
    app = create_app(state, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n  nft-ops {__version__}\n  Dashboard: {url}\n  Data folder: {args.data.resolve()}\n"
          "  Only this computer can open it. Press Ctrl+C to quit.\n")
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, (url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

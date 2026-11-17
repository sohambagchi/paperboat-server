import argparse
import logging

import uvicorn

from . import config


def main() -> None:
    ap = argparse.ArgumentParser(prog="paperboat", description="Run the paperboat collector.")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--data", help=f"data directory (default {config.DATA_DIR})")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = ap.parse_args()
    if args.data:
        config.set_data_dir(args.data)
        import os
        os.environ["PAPERBOAT_DATA"] = str(config.DATA_DIR)  # survives --reload's subprocess
    config.PORT = args.port
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("paperboat.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()

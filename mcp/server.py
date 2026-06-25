#!/usr/bin/env python3
"""Universal entrypoint for the Datasheet MCP server.

Runnable from any working directory (MCP clients spawn the server as a
subprocess and rarely control its cwd). Puts this file's directory on sys.path
so `import ds` resolves, loads mcp/.env, then starts the server.

Usage:
    python /abs/path/to/mcp/server.py
    DS_TRANSPORT=streamable-http python /abs/path/to/mcp/server.py
"""

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
except ImportError:
    pass

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from ds.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()

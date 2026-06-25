"""Pytest configuration — puts mcp/ on sys.path so `import ds` resolves."""
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

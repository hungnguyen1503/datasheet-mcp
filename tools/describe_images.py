#!/usr/bin/env python3
"""Stage 3: VLM figure description for datasheet markdown images.

Scans data/<PART>/MD/**/*.md for bare `![](...)` image references produced by
MinerU, sends each figure to a local VLM (Qwen3-VL-8B via LM Studio or vLLM),
and rewrites the reference with alt-text + structured blockquote.

The blockquote prose becomes searchable text in the ds_prose Qdrant collection,
making datasheet figures (timing diagrams, block diagrams, pinout drawings,
package dimensions, application circuits) discoverable via ds_search.

Backends (auto-detected in order):
  - LM Studio  (http://localhost:1234/v1)
  - vLLM       (http://localhost:8000/v1)

Cache: data/<PART>/MD/.describe_images.json  — resumable, interrupt-safe.

Usage:
  python tools/describe_images.py --part ADXL345
  python tools/describe_images.py --part ADXL345 --workers 8 --retry-failed
  python tools/describe_images.py --part ADXL345 --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import _bootstrap  # noqa: F401 — puts mcp/ on sys.path, loads .env
from _scaffold import md_files, DATA_ROOT

_BARE_IMAGE = re.compile(r'!\[\]\(([^)]+)\)')

# ── Prompt for datasheet figures ──────────────────────────────────────────

_PROMPT = """You are reading an IC datasheet. Describe this figure in detail.
Extract ALL technical information visible:

**Figure type:** Is it a timing diagram, block diagram, pinout/pin-configuration drawing,
package/mechanical drawing, application circuit, register map visualization, flowchart,
waveform, graph/plot, or table? Answer with the single best category.

**Signals / Pins / Nets:** every signal name, pin label, net name, or bus name shown.
Describe what each one represents.

**Timing / Values:** all numerical values — frequencies, periods, voltages, currents,
setup/hold times, pulse widths, bit positions, register addresses, temperatures,
dimensions (mm/mil).

**Blocks / Components:** every named functional block, module, or component visible.
Describe its role briefly.

**Description:** one or two sentences that capture what this figure illustrates and
why it matters to someone integrating this IC.

Output format — use EXACTLY this structure (one line per field, no extra markdown):

Type: <category>
Signals: <list>
Values: <list>
Blocks: <list>
Description: <sentence>"""

# ── VLM client ────────────────────────────────────────────────────────────

def _build_client():
    """Auto-detect a running LM Studio or vLLM instance. Returns (client, backend_name)."""
    from openai import OpenAI

    # Check LM Studio first
    try:
        client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio", timeout=10)
        client.models.list()
        return client, "LM Studio"
    except Exception:
        pass

    # Check vLLM
    try:
        client = OpenAI(base_url="http://localhost:8000/v1", api_key="vllm", timeout=10)
        client.models.list()
        return client, "vLLM"
    except Exception:
        pass

    return None, ""


def _get_or_build_client():
    """Process-wide singleton for the VLM client."""
    global _client, _client_backend
    if _client is None:
        _client, _client_backend = _build_client()
    return _client, _client_backend


_client = None
_client_backend = ""


# ── Core describe logic ───────────────────────────────────────────────────

def describe_image(image_path: Path, *, max_px: int = 1024, model: str = "") -> str:
    """Send one image to the VLM and return the description string.

    Returns empty string on failure (cached as sentinel).
    """
    try:
        from PIL import Image
    except ImportError:
        return ""

    client, backend = _get_or_build_client()
    if client is None:
        return ""

    try:
        img = Image.open(image_path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        # Resize if needed to reduce VLM tokens
        w, h = img.size
        if max(w, h) > max_px:
            scale = max_px / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""

    data_url = f"data:image/png;base64,{b64}"

    # Retry with backoff
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model or "qwen/qwen3-vl-8b",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                temperature=0.15, top_p=0.9, max_tokens=600,
                timeout=300,
            )
            text = resp.choices[0].message.content
            if text and text.strip():
                return text.strip()
            return ""
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return ""


# ── Markdown rewriter ─────────────────────────────────────────────────────

def _build_alt_text(desc: str) -> str:
    """Extract a short alt-text line from the VLM description."""
    for line in desc.splitlines():
        line = line.strip()
        if line.lower().startswith("type:") and len(line) > 6:
            typ = line[5:].strip()
            # Grab signals too if present
            sigs = ""
            for l in desc.splitlines():
                if l.strip().lower().startswith("signals:") and len(l) > 9:
                    sigs = " -- " + l[8:].strip()[:80]
                    break
            return f"{typ}{sigs}"
        if line.lower().startswith("description:") and len(line) > 13:
            return line[12:].strip()[:120]
    return "Figure"


def rewrite_markdown(md_path: Path, rel_path: str, desc: str) -> bool:
    """Replace bare `![](rel_path)` with `![alt](rel_path)\\n\\n> blockquote`."""
    if not desc.strip():
        return False

    alt = _build_alt_text(desc)
    old = md_path.read_text(encoding="utf-8", errors="replace")
    bare = f"![]({rel_path})"
    if bare not in old:
        return False

    new_block = f"![{alt}]({rel_path})\n\n> {desc.replace(chr(10), chr(10) + '> ')}"
    new_text = old.replace(bare, new_block, 1)
    md_path.write_text(new_text, encoding="utf-8")
    return True


# ── Entry point ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="VLM figure descriptions for datasheet markdown")
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--workers", type=int, default=8, help="Parallel VLM workers (default: 8)")
    ap.add_argument("--max-px", type=int, default=1024, help="Max image dimension (default: 1024)")
    ap.add_argument("--model", default="", help="VLM model name (default: auto-detect)")
    ap.add_argument("--limit", type=int, default=0, help="Max images to process (0=all)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be done, don't process")
    ap.add_argument("--retry-failed", action="store_true", help="Retry previously failed images")
    ap.add_argument("--reset", action="store_true", help="Delete cache and re-process all")
    args = ap.parse_args()

    part = args.part

    # Find all markdown files
    mds = md_files(part)
    if not mds:
        print(f"No markdown files found for part '{part}'. Run Stage 1 (pdf_to_md.py) first.")
        sys.exit(1)

    # Scan for bare image references
    images: list[tuple[Path, str, Path]] = []  # (md_path, rel_path, abs_path)
    for md in mds:
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in _BARE_IMAGE.finditer(text):
            rel = m.group(1)
            abs_img = (md.parent / rel).resolve()
            if abs_img.is_file():
                images.append((md, rel, abs_img))

    if not images:
        print(f"No bare image references found in {len(mds)} markdown files for '{part}'.")
        return

    print(f"Found {len(images)} bare image(s) across {len(mds)} markdown file(s) for '{part}'.")

    # Load cache
    md_root = DATA_ROOT / part / "MD"
    cache_path = md_root / ".describe_images.json"
    cache: dict[str, str] = {}
    if cache_path.exists() and not args.reset:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    # Filter images to process
    to_process = []
    for md, rel, abs_img in images:
        key = str(abs_img.relative_to(DATA_ROOT))
        if key in cache and not args.retry_failed:
            cached = cache[key]
            if cached:  # non-empty = success
                continue
            # empty = failed sentinel — skip unless --retry-failed
            if not args.retry_failed:
                continue
        to_process.append((md, rel, abs_img, key))

    if args.limit > 0:
        to_process = to_process[:args.limit]

    already_done = len(images) - len(to_process)
    print(f"Already described: {already_done}, to process: {len(to_process)}")

    if args.dry_run:
        for md, rel, abs_img, key in to_process:
            print(f"  Would describe: {abs_img.name} (in {md.parent.name}/)")
        return

    if not to_process:
        print("All images already described. Use --retry-failed to retry failures.")
        return

    # Check VLM availability
    client, backend = _get_or_build_client()
    if client is None:
        print("No VLM backend found!")
        print("Start LM Studio with qwen3-vl-8b loaded, or vLLM on port 8000.")
        print("Then re-run this script.")
        sys.exit(1)
    print(f"VLM backend: {backend}")

    # Process images
    success = 0
    fail = 0
    lock = threading.Lock()

    def process_one(md, rel, abs_img, key):
        nonlocal success, fail
        desc = describe_image(abs_img, max_px=args.max_px, model=args.model)
        if desc and rewrite_markdown(md, rel, desc):
            with lock:
                cache[key] = desc
                success += 1
            print(f"  OK  {abs_img.name} ({len(desc)} chars)")
        else:
            with lock:
                cache[key] = ""  # failed sentinel
                fail += 1
            print(f"  FAIL  {abs_img.name}")

        # Save cache periodically (every 10 images)
        with lock:
            if (success + fail) % 10 == 0:
                _save_cache(cache_path, cache)

    if args.workers > 1 and len(to_process) > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(process_one, md, rel, abs_img, key)
                       for md, rel, abs_img, key in to_process]
            for f in as_completed(futures):
                f.result()  # re-raise on error
    else:
        for md, rel, abs_img, key in to_process:
            process_one(md, rel, abs_img, key)

    # Save final cache
    _save_cache(cache_path, cache)

    print(f"\nDone: {success} described, {fail} failed, {already_done} already cached.")


def _save_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  [warn] Failed to save cache: {e}")


if __name__ == "__main__":
    main()

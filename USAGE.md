# Datasheet MCP — Usage Guide

## Server connection

| Field | Value |
|---|---|
| **URL** | `https://datasheetmcp.hungnguyenjx.space/mcp` |
| **Transport** | `streamable-http` (HTTP POST + SSE) |
| **Auth** | Bearer token (see below) |

### API Token

```
dcad7617dfd82a4a21c2336b1eaaafe6fb3817f62342122a8cfdd9d7cf9bec81
```

### Client config (.mcp.json)

```json
{
  "mcpServers": {
    "ds": {
      "url": "https://datasheetmcp.hungnguyenjx.space/mcp",
      "headers": {
        "Authorization": "Bearer dcad7617dfd82a4a21c2336b1eaaafe6fb3817f62342122a8cfdd9d7cf9bec81"
      }
    }
  }
}
```

For **local dev** (stdio, no auth):
```json
{
  "mcpServers": {
    "ds": {
      "command": "python",
      "args": ["/path/to/datasheet-mcp/mcp/server.py"]
    }
  }
}
```

---

## Server details

| Field | Value |
|---|---|
| **Host** | `192.168.2.8` (HCM-SERVER-MAIN) |
| **SSH** | `ssh hungnguyen@192.168.2.8` (password: `renesas`) |
| **Service** | `datasheetmcp` (systemd) |
| **Server path** | `/home/hungnguyen/datasheet-mcp/` |
| **Venv** | `/home/hungnguyen/datasheet-mcp/.venv/` |
| **Port** | `8060` |
| **Qdrant** | `localhost:6333` (shared with HUM / Schematic) |
| **Qdrant API key** | `3f99b017871d4f908b955eb2637ef80846139bc6d9ef9824dfcf148683d93cf1` |
| **Cloudflared** | `ds-cloudflared` (docker container, `network_mode: host`) |

### Server management

```bash
# SSH in
ssh hungnguyen@192.168.2.8

# Check service
sudo systemctl status datasheetmcp

# Restart
sudo systemctl restart datasheetmcp

# View logs
journalctl -u datasheetmcp --no-pager -n 50

# Check cloudflared
docker logs ds-cloudflared --tail 20
```

---

## Tools reference

### 1. `ds_list` — Catalog

```
ds_list()                          # List all indexed parts
ds_list("ADXL345")                 # List blocks for a part
```

### 2. `ds_search` — Hybrid search

```
ds_search("ADXL345", "FIFO mode overview")
ds_search("ADXL345", "supply voltage", content_type="spec")
ds_search("ADXL345", "how to configure FIFO", content_type="operation")
ds_search("MX25LM51245G", "ordering information", content_type="order")
```

### 3. `ds_lookup_register` — Register lookup

```
ds_lookup_register("ADXL345", "POWER_CTL")              # Full register card
ds_lookup_register("ADXL345", "POWER_CTL", bit="MEASURE")  # Single bit
ds_lookup_register("ADXL345", "POWER_CTL", bits=False)   # Header only
```

### 4. `ds_find_pin` — Pin finder

```
ds_find_pin("ADXL345")                     # All pins
ds_find_pin("ADXL345", signal="SDA")       # Specific signal
ds_find_pin("ADXL345", block="SERIAL")     # Filter by block
```

### 5. `ds_neighbors` — Dependency graph

```
ds_neighbors("ADXL345", "FIFO")               # Block neighborhood
ds_neighbors("ADXL345", "POWER_CTL", depth=1) # Register neighborhood
```

### 6. `ds_auto` — Auto-router

```
ds_auto("ADXL345", "how do I configure the FIFO?")
ds_auto("ADXL345", "which pin is SDA?")
ds_auto("ADXL345", "what is the supply voltage?")
ds_auto("ADXL345", "POWER_CTL")
```

---

## Ingestion pipeline

### Quick (Windows with GPU)

```bash
# Full pipeline for one part (interactive)
build_all.bat --part ADXL345

# Full pipeline, non-interactive
build_all.bat --part ADXL345 --yes

# Re-index only (after model change)
build_all.bat --part ADXL345 --index-only --reset
```

### Manual stage-by-stage

```bash
# Stage 1: PDF → Markdown via MinerU (GPU, hybrid-engine, effort=high)
python tools/pdf_to_md.py --pdf /path/to/Part.pdf --part PartName

# Stage 2: Markdown → JSON (heuristic, instant, no LLM)
python tools/extract_structured.py --part PartName

# Stage 3: VLM figure descriptions (optional, needs LM Studio)
python tools/describe_images.py --part PartName --workers 8

# Stage 4: Index to Qdrant
cd mcp && build.bat --part PartName
```

### Zero-interaction (CI-ready)

```bash
python tools/pipeline.py --part ADXL345 --yes
```

### Deploy to server (Windows → Ubuntu)

```bash
# After Stages 1-3 on Windows, copy to server:
scp -r datasheet/<PART> hungnguyen@192.168.2.8:/home/hungnguyen/datasheet-mcp/datasheet/

# SSH to server and run Stage 4:
ssh hungnguyen@192.168.2.8
cd /home/hungnguyen/datasheet-mcp/mcp
bash build.sh --part <PART>
```

---

## Qdrant collections

| Collection | Type | Content |
|---|---|---|
| `ds_registers` | Dense vector (384-dim) | Register cards with bitfields |
| `ds_prose` | Dense + sparse BM25 | Heading-scoped prose blocks with content_type |
| `ds_pins` | Payload-only | Pin/signal descriptions |
| `ds_graph` | Payload-only | Dependency graph edges |
| `ds_catalog` | Payload-only | Part metadata (vendor, blocks, revision) |

---

## content_type classification

Prose blocks are auto-classified during ingestion:

| content_type | Heading patterns | Example headings |
|---|---|---|
| `operation` | "operation", "initialization", "configuration", "sequence", "start-up", "read/write" | "FIFO Operation", "Power-Up Sequence", "SPI Read Command" |
| `spec` | "specification", "electrical characteristic", "absolute maximum", "timing", "DC/AC characteristic" | "Electrical Characteristics", "Absolute Maximum Ratings", "AC Timing" |
| `order` | "ordering", "part number", "package code", "order code", "part marking" | "Ordering Information", "Package Marking", "Part Number Decoder" |
| `general` | everything else | "Features", "Overview", "Functional Description" |

---

## Test connectivity

```bash
# Test via cloudflared
curl -X POST https://datasheetmcp.hungnguyenjx.space/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer dcad7617dfd82a4a21c2336b1eaaafe6fb3817f62342122a8cfdd9d7cf9bec81" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Test ds_list
curl -X POST https://datasheetmcp.hungnguyenjx.space/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer dcad7617dfd82a4a21c2336b1eaaafe6fb3817f62342122a8cfdd9d7cf9bec81" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ds_list","arguments":{}}}'
```

# Datasheet MCP — Usage Guide

## Server connection

| Field | Value |
|---|---|
| **URL** | `https://datasheetmcp.hungnguyenjx.space/mcp` |
| **Transport** | `streamable-http` (HTTP POST + SSE) |
| **Auth** | Bearer token (see below) |

### API Token

```
<token>
```

### Client config (.mcp.json)

```json
{
  "mcpServers": {
    "ds": {
      "url": "https://datasheetmcp.hungnguyenjx.space/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
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

### 1. `ds_catalog` — Catalog and coverage

```
ds_catalog()                          # List all indexed parts
ds_catalog("ADXL345")                # Outline and extraction coverage
```

### 2. `ds_query` — Implementation packet

```
ds_query("MX25LM51245G", "How do I configure SPI mode?")
ds_query("MX25LM51245G", "Set dummy cycles for 133 MHz", focus="timing")
ds_query("MX25LM51245G", "Program/Erase flow without array reads", focus="operation")
```

### 3. `ds_get` — Exact entity lookup

```
ds_get("ADXL345", "POWER_CTL")
ds_get("MX25LM51245G", "DC[2:0]", relation_depth=2)
ds_get("MX25LM51245G", "ds://MX25LM51245G/.../table/...")
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
| `ds_evidence` | Dense 768-dim + sparse BM25 | Source-linked tables, registers, commands, modes, timing, operations, pins, and constraints |
| `ds_graph` | Payload-only | Typed evidence relationships |
| `ds_catalog` | Payload-only | Part metadata, hierarchy, and extraction coverage |

---

## Evidence classification

Evidence is classified during ingestion into explicit entity kinds:

| Kinds | Examples |
|---|---|
| Structure | document, chapter, section, table, table row, figure |
| Configuration | register, bitfield, mode, command, pin, memory region |
| Behavior | operation, step, parameter, constraint, warning, prose |

---

## Test connectivity

```bash
# Test via cloudflared
curl -X POST https://datasheetmcp.hungnguyenjx.space/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer <token>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Test ds_catalog
curl -X POST https://datasheetmcp.hungnguyenjx.space/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer <token>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ds_catalog","arguments":{}}}'
```

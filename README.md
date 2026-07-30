# 📋 Datasheet MCP Server

Turns multi-page IC datasheets into part-scoped, source-linked implementation
evidence for embedded development. Tables, commands, modes, timing, operations,
registers, pins, and graph relationships are retrieved with Qdrant hybrid search.

---

## ⚡ Quick start

```bash
# 1. Clone and install
git clone https://github.com/hungnguyen1503/datasheet-mcp.git
cd datasheet-mcp
pip install -r mcp/requirements.txt

# 2. Add to your MCP client (.mcp.json or Claude Desktop config):
# {
#   "mcpServers": {
#     "ds": {
#       "url": "https://datasheetmcp.hungnguyenjx.space/mcp",
#       "headers": { "Authorization": "Bearer <token>" }
#     }
#   }
# }

# 3. Start querying:
#    ds_query("MX25LM51245G", "How do I configure SPI mode?")
#    ds_query("MX25LM51245G", "Set dummy cycles for 133 MHz", focus="timing")
#    ds_get("ADXL345", "POWER_CTL")
```

To **add your own datasheet**, drop the PDF in a folder and run:

```bash
python tools/ingest.py --pdf /path/to/Part.pdf
```

That's it — the folder, markdown, and Qdrant index are created automatically.

---

## 🏗️ Architecture

```mermaid
graph TB
    subgraph Build["🖥️ Build Machine  (one-time ingestion)"]
        PDF[/"📄 Datasheets<br/>sensor.pdf · flash.pdf · camera.pdf · …"/]
        GPU{"CUDA?"}
        S1H["MinerU hybrid-engine<br/>(VLM table understanding)"]
        S1P["MinerU pipeline<br/>(text-only · CPU-safe)"]
        MD[/"📁 datasheet/PART/MD/<br/>chapter markdown"/]
        S2["⚙️ Stage 2<br/>Heuristic table parser<br/>no LLM required"]
        JSON[/"registers.json<br/>pins.json · catalog.json"/]
        S3["🎨 Stage 3 (optional)<br/>VLM figure descriptions<br/>Qwen3-VL-8B"]
        S4["⚙️ Stage 4<br/>build.bat<br/>embed + push to Qdrant"]
    end

    subgraph DB["🗄️ Qdrant  (remote · shared instance)"]
        E["ds_evidence<br/>dense 768 + sparse BM25<br/>RRF fusion"]
        G["ds_graph<br/>payload-only relations"]
        C["ds_catalog<br/>payload-only coverage"]
    end

    subgraph Server["⚡ MCP Server  (streamable-http)"]
        SRV["mcp/server.py<br/>FastMCP · stateless_http"]
        TOOLS["3 ds_* tools"]
    end

    subgraph Clients["💬 MCP Clients"]
        CLI["Claude Code · Claude Desktop<br/>Cursor · Cline"]
    end

    PDF --> GPU
    GPU -- "Yes" --> S1H --> MD
    GPU -- "No"  --> S1P --> MD
    MD --> S2 --> JSON --> S4
    MD -->|"prose blocks"| S3 -->|"enriched markdown"| S4
    S4 -->|"evidence · graph · coverage"| E & G & C
    E & G & C -->|"query"| SRV
    SRV --> TOOLS -->|"tool calls"| CLI
```

> **No CLIP, no visual search.** All retrieval is text-only — dense BGE vectors + BM25
> sparse vectors with RRF fusion. The `hybrid-engine` label refers to MinerU's internal
> PDF parsing strategy (text + layout + table VLM), **not** cross-modal image/text embedding.

---

## 📡 Deployment topology

```mermaid
flowchart LR
    subgraph Win["Windows PC  (GPU)"]
        MinerU["MinerU<br/>hybrid-engine"]
        VLM["VLM<br/>Qwen3-VL-8B"]
        Build["build_all.bat<br/>Stages 1→3"]
    end

    subgraph Svr["Ubuntu Server  (CPU)"]
        Qdrant[("Qdrant<br/>:6333")]
        DS["datasheetmcp<br/>systemd · :8060"]
        CF["cloudflared<br/>docker"]
    end

    subgraph CF["Cloudflare"]
        Edge["Zero Trust<br/>Tunnel"]
    end

    subgraph Client["MCP Clients"]
        CC["Claude Code"]
    end

    Win -- "SCP datasheet/ → server" --> Svr
    Qdrant --- DS
    DS --- CF
    CF -- "QUIC" --> Edge
    Edge -- "HTTPS" --> Client
    Client -- "tools/call" --> Edge
```

- **Stages 1–3** run on a GPU machine (MinerU PDF→MD, VLM figure descriptions)
- **Stage 4** runs on the server (embed + push to Qdrant)
- The MCP server runs on **port 8060**, exposed via **Cloudflare Tunnel**
- Qdrant is shared with the HUM MCP and Schematic MCP servers

---

## 🛠️ The tools

Three tools cover discovery, implementation questions, and exact retrieval.

| Tool | Args | What it does |
|---|---|---|
| `ds_catalog` | `[part]`, `cursor`, `limit` | List parts or inspect one part's outline and extraction coverage |
| `ds_query` | `part`, `question`, `focus`, `max_tokens` | Return normalized settings, ordered steps, exact facts, constraints, relations, sources, and explicit gaps |
| `ds_get` | `part`, `target`, `relation_depth` | Resolve one exact ID/symbol/table/command/register and its bounded graph context |

> ⚠️ **`part` is required on every call except `ds_catalog()`.** This prevents identically
> named registers on different ICs from ever mixing up their data.

`ds_query.focus` accepts `auto`, `configure`, `exact`, `operation`, `timing`, or
`explain`. Query assembly is deterministic and reports incomplete evidence in `gaps`.

---

## 📦 Pipeline

Full 4-stage ingestion pipeline for one or more IC datasheet PDFs.

### Stage overview

| Stage | Script | What it does | ML? |
|---|---|---|---|
| **1** | `tools/pdf_to_md.py` | PDF → chapter markdown via MinerU (hybrid-engine on GPU, pipeline on CPU) | GPU |
| **2** | `tools/extract_structured.py` | Markdown → registers.json + pins.json (heuristic, no LLM) | — |
| **3** | `tools/describe_images.py` | VLM figure descriptions for timing diagrams, block diagrams, pinouts (optional) | GPU VLM |
| **4** | `mcp/build.bat` / `mcp/build.sh` | Embed + push to Qdrant | — |

### Quick run

```bash
# Interactive — pick which PDF to ingest
build_all.bat

# Single part, full pipeline
build_all.bat --part ADXL345

# Index only (re-index after model change)
build_all.bat --part ADXL345 --index-only

# Non-interactive
build_all.bat --part ADXL345 --yes
```

### Manual stage-by-stage

```bash
# Stage 1: PDF → Markdown (MinerU, hybrid-engine on GPU)
python tools/pdf_to_md.py --pdf /path/to/Part.pdf

# Stage 2: Markdown → JSON (heuristic, instant)
python tools/extract_structured.py --part PartName

# Stage 3: VLM figure descriptions (optional, requires LM Studio with Qwen3-VL-8B)
python tools/describe_images.py --part PartName

# Stage 4: Index to Qdrant
cd mcp && build.bat --part PartName

# Artifact-only inspection; does not mutate Qdrant
python tools/extract_evidence.py --part PartName --no-enrich

# After indexing, run the golden retrieval/latency gates
python tools/evaluate_retrieval.py --strict
```

### Resumability

Each stage checks whether its output already exists and skips automatically:
- **Stage 1**: `datasheet/<PART>/MD/` exists and non-empty
- **Stage 2**: `datasheet/<PART>/registers.json` exists
- **Stage 3**: `.describe_images.json` cache exists with all images processed
- **Stage 4**: Data present in Qdrant collections

Use `pipeline.py` for zero-interaction runs with sentinel-based completion tracking:

```bash
python tools/pipeline.py --part ADXL345 --yes
```

---

## ⚙️ Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DS_TRANSPORT` | `streamable-http` | MCP transport: `stdio` / `streamable-http` |
| `DS_HOST` | `127.0.0.1` | Host for HTTP transport |
| `DS_PORT` | `8060` | Port for HTTP transport |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |
| `QDRANT_API_KEY` | *(set in deploy)* | Qdrant API key |
| `DS_API_KEYS` | *(unset)* | Comma-separated bearer tokens for HTTP auth |
| `DS_EMBED_MODEL` | `BAAI/bge-base-en-v1.5` | Sentence-transformers model (768-dim) |
| `DS_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Optional cross-encoder reranker |
| `MINERU_DEVICE_MODE` | auto | MinerU device: `cuda` / `cpu` |

### Embedding model options

| Model | Dim | Size | Best for |
|---|---|---|---|
| `BAAI/bge-base-en-v1.5` **(default)** | 768 | ~440 MB | CPU with ≥ 16 GB RAM |
| `BAAI/bge-small-en-v1.5` | 384 | ~130 MB | Requires a full collection rebuild |
| `BAAI/bge-large-en-v1.5` | 1024 | ~1.3 GB | GPU recommended |

---

## 🗄️ Qdrant collections

| Collection | Vectors | Key fields | Used by |
|---|---|---|---|
| `ds_evidence` | dense 768-dim + sparse BM25 | part, kind, title, values, table, provenance | `ds_query`, `ds_get` |
| `ds_graph` | payload-only | part, relation, source_id, target_id | `ds_query`, `ds_get` |
| `ds_catalog` | payload-only | part metadata, outline, extraction coverage | `ds_catalog` |

---

## 📁 Project structure

```
datasheet-mcp/
├── .mcp.json                  ← MCP client registration (cloudflared URL)
├── build_all.bat / .sh        ← Full pipeline entry point
├── deploy/
│   ├── docker-compose.yml     ← Cloudflared tunnel container
│   ├── datasheetmcp.service   ← systemd service unit
│   └── .env.example           ← Deployment env template
├── mcp/
│   ├── server.py              ← MCP server entrypoint
│   ├── build.bat / .sh        ← Stage 4 entry point (Qdrant push)
│   ├── build_helper.py        ← Qdrant collection manager
│   ├── requirements.txt
│   ├── .env.example
│   └── ds/                    ← main Python package
│       ├── mcp_server.py      ← FastMCP tool definitions (3 tools)
│       ├── evidence/          ← lossless parser, store, graph, service contracts
│       ├── model.py           ← RegisterCard, Pin, ProseBlock, …
│       ├── embed.py           ← GPU/CPU adaptive embedder
│       ├── collections.py     ← Qdrant collection prefix helper
│       ├── catalog.py         ← part/section discovery
│       ├── cards.py           ← register card renderer
│       ├── index/             ← Qdrant store wrappers
│       │   ├── regstore_qdrant.py
│       │   ├── prose_qdrant.py
│       │   └── pins_qdrant.py
│       ├── ingest/            ← ingestion pipeline
│       │   ├── extract.py     ← heuristic table parser
│       │   ├── prose.py       ← markdown → ProseBlock
│       │   └── build.py       ← JSON → Qdrant orchestrator
│       └── graph/             ← dependency graph
│           ├── model.py · store_qdrant.py · build.py · query.py
├── tools/
│   ├── _bootstrap.py          ← shared sys.path + .env bootstrap
│   ├── _scaffold.py           ← part discovery, directory setup
│   ├── build_all.py           ← Full 4-stage pipeline orchestrator
│   ├── pipeline.py            ← Zero-interaction pipeline (CI-ready)
│   ├── ingest.py              ← Interactive PDF picker + pipeline
│   ├── pdf_to_md.py           ← Stage 1: PDF → Markdown (MinerU)
│   ├── extract_structured.py  ← Stage 2: Markdown → JSON
│   └── describe_images.py     ← Stage 3: VLM figure descriptions
├── datasheet/                 ← per-part data (gitignored)
│   └── <PART>/
│       ├── source.pdf
│       ├── MD/                ← MinerU markdown
│       ├── registers.json     ← Stage 2 output
│       ├── pins.json
│       └── catalog.json
└── tests/                     ← unit tests
```

---

## 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| ❌ `ds_catalog()` returns empty | No parts indexed — run the pipeline |
| ❌ `MinerU not found` | `pip install mineru` or use `--backend pymupdf` |
| ❌ Cannot connect to Qdrant | Check `QDRANT_URL` in `mcp/.env`; ensure Qdrant is running |
| ⚠️ `ds_query` is slow on first call | The embedding and sparse models are loading; use server prewarm. |
| ⚠️ 0 registers extracted | Non-standard table headers — prose search still finds the content |
| ⚠️ `ds_query` reports gaps | Inspect `ds_catalog(part)` coverage and rebuild the evidence corpus. |
| ❌ Build fails: `No markdown found` | Stage 1 not run — `python tools/pdf_to_md.py --part <P>` first |

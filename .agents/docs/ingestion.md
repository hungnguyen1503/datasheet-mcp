# Ingestion Pipeline

## Inputs and outputs

The pipeline discovers PDFs in the repository root or `datasheet/<PART>/source.pdf`. A part directory contains regenerated artifacts:

```text
datasheet/<PART>/
├── source.pdf
├── MD/                 # Stage 1 markdown and images
├── registers.json      # Stage 2 register cards
├── pins.json           # Stage 2 pin records
├── catalog.json        # Stage 2 part metadata
└── evidence/corpus.json # Evidence, graph, coverage, and manifest
```

Source PDFs, markdown, and extracted JSON are ignored by Git. Keep them local unless the user explicitly asks to version a specific artifact.

## Stages

| Stage | Entry point | Result | Environment |
|---|---|---|---|
| 1 | `python tools/pdf_to_md.py --pdf <PDF> --part <PART>` | MinerU PDF-to-markdown output | MinerU; CUDA preferred, CPU backend available |
| 2 | `python tools/extract_structured.py --part <PART>` | `registers.json`, `pins.json`, `catalog.json` | Heuristic parser; no LLM |
| 3 | `python tools/describe_images.py --part <PART> --workers 8` | VLM descriptions in markdown/cache | Optional; requires a configured VLM such as LM Studio |
| 4 | `mcp\build.bat --part <PART>` or `bash mcp/build.sh --part <PART>` | Evidence extraction, 768-d embeddings, and Qdrant upserts | Reachable Qdrant and embedding dependencies |

Use the orchestrators for normal work:

```text
Windows:    build_all.bat --part ADXL345 --yes
Unix:       bash build_all.sh --part ADXL345 --yes
Auto-resume: python tools/pipeline.py --part ADXL345 --yes
Index-only:  python tools/pipeline.py --part ADXL345 --only 4 --yes
Artifact only: python tools/extract_evidence.py --part ADXL345 --no-enrich
```

`build_all.py` skips stages when expected outputs exist. `pipeline.py` additionally records `.stage_<N>_done` sentinels and resumes from the first missing sentinel. A sentinel or `catalog.json` is only a local completion heuristic; verify Qdrant when diagnosing an indexing problem.

## Flags and safety

- `--from 2` starts the orchestrated run at Stage 2; `--only 4` runs one stage.
- `--no-describe` omits optional figure enrichment. Prose and graph evidence are mandatory; `--no-prose` and `--no-graph` are rejected by the canonical builder.
- `--reset` drops the three canonical collections; it is global and requires explicit authorization.
- Changing `DS_EMBED_MODEL` can change vector dimensions. Plan a reset and complete re-index; do not mutate a populated collection in place.

If Stage 1 fails, use the configured MinerU backend or `--mineru-backend pipeline` for the CPU-oriented path. If Stage 4 fails, check `mcp/.env`, Qdrant reachability, collection prefix, and model downloads before changing code.

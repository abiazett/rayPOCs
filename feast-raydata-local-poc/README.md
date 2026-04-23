# Feast + RayData + Docling: Local RAG POC

Local proof-of-concept that combines Feast (feature store), Ray Data (distributed processing), and Docling (PDF parsing) into a RAG pipeline.

This validates the same architecture that runs on RHOAI, but locally without Kubernetes.

## Architecture

```
PDFs (local folder)
    |
    v
Step 1: parse_and_chunk (Ray Data + Docling)
    RayData actors with subprocess isolation
    Docling parse -> HybridChunker -> sentence-transformers embed
    -> Parquet (Feast-compatible schema)
    |
    v
Step 2: feast_materialize
    feast apply -> write_to_online_store()
    -> Milvus Lite (embedded, file-based)
    |
    v
Step 3: rag_query
    Embed question -> retrieve_online_documents_v2()
    -> Context -> LLM (OpenAI API, optional)
```

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Add sample PDFs to the pdfs/ folder
# (any PDF files will work)
```

## Run

### Option 1: Sequential (simplest)

```bash
python run_local.py
```

Or run each step individually:

```bash
python step1_process.py          # Parse, chunk, embed -> Parquet
python step2_materialize.py      # Feast -> Milvus Lite
python step3_query.py "What are the main topics?"  # RAG query
```

### Option 2: KFP Local Runner

```bash
RUN_MODE=kfp python run_local.py
```

This uses `kfp.local.LocalRunner` to execute the pipeline DAG, validating the same orchestration that would run on RHOAI with Data Science Pipelines.

### Option 3: Compile for RHOAI

```bash
python pipeline.py   # Generates rag_pipeline.yaml
```

Upload `rag_pipeline.yaml` to the Data Science Pipelines UI on RHOAI.

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|---|---|---|
| `PDF_DIR` | `pdfs` | Folder containing input PDFs |
| `OUTPUT_PARQUET` | `feature_repo/data/processed_chunks.parquet` | Output path |
| `EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model |
| `CHUNK_MAX_TOKENS` | `256` | Max tokens per chunk |
| `NUM_FILES` | `0` (all) | Limit number of PDFs |
| `MAX_ACTORS` | `2` | Max Ray Data actors |
| `CPUS_PER_ACTOR` | `2` | CPUs per actor |
| `OPENAI_API_KEY` | (none) | For LLM answers (optional) |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `RUN_MODE` | `sequential` | `sequential` or `kfp` |

## Project Structure

```
feast-raydata-local-poc/
+-- pdfs/                          # Put your PDFs here
+-- feature_repo/
|   +-- feature_store.yaml         # Feast config (local + Milvus Lite)
|   +-- definitions.py             # FeatureView + Entity definitions
|   +-- data/                      # Generated: registry.db, online_store.db, parquet
+-- step1_process.py               # RayData + Docling -> Parquet
+-- step2_materialize.py           # Feast -> Milvus Lite
+-- step3_query.py                 # Feast vector search -> RAG
+-- pipeline.py                    # KFP pipeline (local + RHOAI)
+-- run_local.py                   # Orchestrator (sequential or KFP local)
+-- requirements.txt
+-- README.md
```

## From Local to RHOAI

The same pipeline runs on RHOAI with these changes:

| Local | RHOAI |
|---|---|
| `ray.init()` local | RayJob CR via CodeFlare SDK |
| Milvus Lite (file) | Milvus on OpenShift |
| Local PDFs folder | PVC with PDFs |
| Local Parquet | S3/MinIO |
| OpenAI API | vLLM InferenceService |
| `kfp.local.LocalRunner` | Data Science Pipelines (DSPA) |

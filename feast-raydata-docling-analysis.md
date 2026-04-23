# Feast + RayData + Docling: Architecture Analysis

## Overview

This document captures the analysis of combining **Feast** (feature store), **Ray Data** (distributed processing), and **Docling** (PDF parsing) into a unified RAG pipeline on Red Hat OpenShift AI (RHOAI).

Three existing implementations were reviewed to inform the design:

1. **RayData + Docling notebook** (`examples/docling/ray-cluster-docling-batch-processing.ipynb`) — Distributed PDF processing at scale on RHOAI
2. **Feast rag-docling example** (`feast-repo/examples/rag-docling/`) — Feast-managed RAG with Docling transformations and Milvus
3. **Saad's KFP pipeline** (`ray-docling-kfp-rag-example/rag-example/`) — KFP-orchestrated RAG pipeline using RayData for document processing

---

## Source Repositories

| Source | Location |
|---|---|
| RayData + Docling notebook | `examples/docling/ray-cluster-docling-batch-processing.ipynb` |
| Feast rag-docling example | `feast-repo/examples/rag-docling/` (cloned from `github.com/feast-dev/feast`) |
| Feast + Ray blog | https://feast.dev/blog/feast-ray-distributed-processing/ |
| Saad's KFP pipeline | `ray-docling-kfp-rag-example/rag-example/` (cloned from `github.com/szaher/ray-docling-kfp-rag-example`) |

---

## Comparison of Existing Approaches

| Aspect | RayData Notebook | Feast rag-docling | Saad's KFP Pipeline |
|---|---|---|---|
| **Orchestration** | Manual (notebook) | None (single notebook) | KFP (5-step DAG) |
| **PDF Parsing** | RayData + Docling (distributed, subprocess isolation) | Docling (sequential, single-threaded) | RayData + Docling (distributed, subprocess isolation) |
| **Chunking** | None (outputs raw Markdown/JSON) | HybridChunker (inside Feast transformation) | HybridChunker (inside RayJob, same actor as parsing) |
| **Embedding** | None | sentence-transformers (in Feast `write_to_online_store`) | Separate KFP step (local or deployed service) |
| **Vector Store** | None (files on PVC) | Milvus via Feast online store | Milvus (direct pymilvus) |
| **Intermediate Storage** | PVC (Markdown/JSON files) | Parquet (Feast offline store) | S3/MinIO (JSONL files) |
| **Feature Management** | None | Feast (FeatureView, versioning, retrieval API) | None |
| **LLM Serving** | None | OpenAI API (external) | vLLM InferenceService on RHOAI (KServe) |
| **Query Interface** | None | Feast `retrieve_online_documents_v2` | pymilvus search + OpenAI-compatible vLLM |
| **Fault Tolerance** | Subprocess timeout, MAX_ERRORED_BLOCKS | None | Subprocess timeout, MAX_ERRORED_BLOCKS |
| **Scalability** | 10K+ PDFs | ~10 PDFs | 1000+ PDFs |

---

## Feast Ray Engine: Deep Dive

### What It Distributes

Feast's Ray compute engine (`batch_engine: ray` in `feature_store.yaml`) uses Ray Datasets with `map_batches()` for:

- **Materialization** — `store.materialize()` distributes via a DAG of Ray nodes
- **Historical retrieval** — `get_historical_features()` does distributed joins and aggregations
- **Batch FeatureView transformations** — custom Python functions run distributed across workers

Key source files:
- Ray engine: `sdk/python/feast/infra/compute_engines/ray/compute.py`
- Ray nodes (map_batches logic): `sdk/python/feast/infra/compute_engines/ray/nodes.py`
- Config: `sdk/python/feast/infra/compute_engines/ray/config.py`

### What It Does NOT Distribute

**OnDemandFeatureView transformations run single-threaded on the client**, regardless of engine configuration. When `write_to_online_store(transform_on_write=True)` is called, the transformation iterates row-by-row in the calling process. The Ray engine is never involved.

Source: `sdk/python/feast/feature_store.py` (`_transform_on_demand_feature_view_df`)

### Configuration

```yaml
batch_engine:
  type: ray.engine
  ray_address: null          # Local if null, or "ray://host:10001" for remote
  use_kuberay: false         # Enable for KubeRay/CodeFlare on RHOAI
  max_workers: null          # None = use all available CPUs
  broadcast_join_threshold_mb: 100
  target_partition_size_mb: 64
```

### Capabilities Summary

| Capability | Supported |
|---|---|
| Batch FeatureView materialization with Ray | Yes |
| Point-in-time joins at scale | Yes |
| Custom Ray transformations (`mode="ray"`) | Yes |
| KubeRay / CodeFlare on RHOAI | Yes |
| OnDemand transformations via Ray | No (client-side only) |
| Subprocess isolation for heavy processing | No (not built-in) |
| GPU actor support | No (Ray supports it, Feast doesn't configure it) |

---

## Two Implementation Options

### Option 1: Extend Feast's Ray Transformation (Pure Feast)

Use a `BatchFeatureView` with `mode="ray"` where the UDF receives and returns a Ray Dataset directly. This would allow plugging in the DoclingProcessor actor inside Feast's transformation layer.

```python
@batch_feature_view(
    sources=[pdf_source],
    mode="ray",
)
def docling_features(dataset: ray.data.Dataset) -> ray.data.Dataset:
    return dataset.map_batches(
        DoclingProcessor,
        batch_format="pandas",
        compute=ray.data.ActorPoolStrategy(min_size=2, max_size=12),
        num_cpus=4,
    )
```

**Pros:**
- Everything managed by Feast — single system for processing, storage, and serving
- Feast handles schema, versioning, and materialization lifecycle
- Uses Feast's native KubeRay support

**Cons:**
- No built-in subprocess isolation (would need custom actor code)
- No built-in per-file timeout or MAX_ERRORED_BLOCKS fault tolerance
- Extending Feast's transformation layer is non-trivial
- Not yet proven at scale with heavy document processing

**Status:** Not a priority for the current POC, but worth revisiting for a tighter integration.

### Option 2: Two-Stage Pipeline (Recommended for POC)

Keep the proven RayData processing pipeline for Stage 1 and use Feast for Stage 2 (feature lifecycle and serving). This mirrors Saad's KFP pipeline architecture but replaces `ingest_to_milvus` with Feast.

**This is the recommended approach for the POC.**

---

## Recommended Architecture (Option 2)

### Pipeline Flow

```
KFP Pipeline (or standalone notebook)
|
+-- Step 1: parse_and_chunk (RayJob with Ray Data)
|     PDFs (PVC) -> Docling + HybridChunker (distributed, subprocess isolation)
|     -> Parquet on S3 (Feast-compatible schema:
|        document_id, chunk_id, file_name, chunk_text, vector, created)
|
+-- Step 2: feast_materialize (replaces ingest_to_milvus)
|     feast apply (register FeatureView pointing to S3 Parquet)
|     store.write_to_online_store() or feast materialize
|     -> Milvus online store
|
+-- Step 3: download_model (from Saad's pipeline)
|     HuggingFace model -> PVC cache
|
+-- Step 4: deploy_model (from Saad's pipeline)
      vLLM InferenceService on RHOAI (KServe)

Query:
  store.retrieve_online_documents_v2() -> context -> vLLM -> answer
```

### What Changes vs. Saad's KFP Pipeline

| Saad's KFP Step | Feast Equivalent | Change Required |
|---|---|---|
| `parse_and_chunk` (RayJob) | **Keep as-is** | Output Parquet instead of JSONL (Feast-compatible schema) |
| `ingest_to_milvus` (single KFP pod) | **Replace with Feast** — `FileSource` -> `FeatureView` -> `materialize` to Milvus | New KFP component or notebook step |
| Direct pymilvus search in query notebook | **Replace with** `store.retrieve_online_documents_v2()` | Update query notebook |
| No versioning or lifecycle management | **Feast adds** feature versioning, TTL, declarative schema | Feast feature repo config |

### What Stays the Same

- **Step 1 (parse_and_chunk):** The RayJob with Ray Data + Docling + HybridChunker stays exactly the same. It uses `ray.data.from_pandas()`, `repartition()`, and `map_batches()` with `ActorPoolStrategy` for distributed processing with subprocess isolation. This is the proven, battle-tested component.
- **Steps 3-4 (model download + deployment):** Keep from Saad's pipeline unchanged.

### What Feast Adds

- **Declarative feature definitions** — schema defined in Python, versioned in git
- **Materialization lifecycle** — `feast apply` + `materialize` manages the offline-to-online flow
- **Vector search API** — `retrieve_online_documents_v2()` with configurable distance metrics
- **TTL and freshness** — automatic expiration of stale features
- **Feature reuse** — teams can discover and share feature views across projects
- **Dual retrieval** — both vector search and structured feature lookups in one call

### Feast Feature Store Configuration

```yaml
# feature_store.yaml
project: rag_docling
provider: local
registry: data/registry.db
online_store:
  type: milvus
  host: milvus-milvus.milvus.svc.cluster.local
  port: 19530
  embedding_dim: 768
  index_type: IVF_FLAT
offline_store:
  type: file  # or ray for large-scale materialization
entity_key_serialization_version: 3
```

### Feast Feature Definition

```python
# example_repo.py
from feast import Entity, FeatureView, Field, FileSource
from feast.types import Float64, Array, String, Int64, ValueType

chunk = Entity(
    name="chunk_id",
    value_type=ValueType.STRING,
    join_keys=["chunk_id"],
)

source = FileSource(
    path="s3://rag-chunks/chunks/output.parquet",
    timestamp_field="created",
)

rag_feature_view = FeatureView(
    name="rag_documents",
    entities=[chunk],
    schema=[
        Field(name="file_name", dtype=String),
        Field(name="chunk_text", dtype=String),
        Field(name="chunk_index", dtype=Int64),
        Field(
            name="vector",
            dtype=Array(Float64),
            vector_index=True,
            vector_search_metric="COSINE",
        ),
    ],
    source=source,
    ttl=timedelta(hours=24),
)
```

---

## Prerequisites for the POC

| Requirement | Status (ana-rosa5 cluster) | Notes |
|---|---|---|
| RHOAI with DSPA | Ready (testana namespace) | |
| KubeRay operator | Ready (via RHOAI) | |
| Ray CRDs | Ready | RayCluster, RayJob, RayService installed |
| GPU nodes | Ready (1 node) | For vLLM serving |
| Pipeline SA | Ready (pipeline-runner-dspa) | |
| Milvus | Not installed | Deploy via `milvus-ocp/` or Milvus Operator |
| MinIO / S3 | Not installed | Deploy via `minio.yaml` in Saad's repo |
| PVC with input PDFs | Partial | Existing PVCs in testana, need PDF data |
| Feast | Not installed | `pip install feast[milvus]` in workbench |
| S3 credentials Secret | Not created | `minio-secret` with access_key/secret_key |
| RBAC for pipeline SA | Not applied | `manifests/rbac.yaml` |

---

## Open Design Decisions

1. **Output format from Step 1** — Parquet (Feast-native) vs. JSONL (Saad's current format). Parquet is preferred for Feast compatibility; would require a minor change to the `_converter_worker` in parse_and_chunk.

2. **Embedding location** — Embed in Step 1 (inside the RayJob, same pass as parsing/chunking) or in Step 2 (during Feast materialization). Embedding in Step 1 keeps one distributed pass and avoids loading the model again. Embedding in Step 2 is more Feast-native but adds a second processing step.

3. **Milvus deployment** — Milvus Lite (local file, zero infra, good for POC) vs. full Milvus on OpenShift (production). For initial POC, Milvus Lite avoids the Milvus Operator setup.

4. **Namespace** — Use existing `testana` (DSPA already configured) or create `ray-docling` (matches Saad's defaults).

5. **Feast offline store backend** — Local Parquet files vs. S3/MinIO. S3 is preferred for persistence and alignment with Saad's pipeline.

---

## Local POC: Implementation Summary

A local proof-of-concept was built in `feast-raydata-local-poc/` to validate Option 2 end-to-end before deploying on RHOAI.

### What Was Built

| File | Purpose |
|---|---|
| `step1_process.py` | RayData + Docling processing with subprocess isolation → Parquet |
| `step2_materialize.py` | Feast `apply` + `write_to_online_store` → Milvus Lite |
| `step3_query.py` | Feast `retrieve_online_documents_v2` vector search + optional OpenAI LLM |
| `pipeline.py` | KFP pipeline with 3 `@dsl.component` functions, compilable to YAML for RHOAI |
| `run_local.py` | Orchestrator: sequential mode or KFP LocalRunner mode |
| `feature_repo/feature_store.yaml` | Feast config: local provider, Milvus Lite online store (384-dim, FLAT index) |
| `feature_repo/definitions.py` | Entity (`chunk_id`), FileSource, FeatureView with COSINE vector_index |

### Test Results

Both execution modes passed successfully with 3 sample PDFs:

| Mode | Step 1 | Step 2 | Step 3 |
|---|---|---|---|
| Sequential (`python3 run_local.py`) | 86 chunks from 3 PDFs | 86 vectors materialized to Milvus Lite | 5 chunks retrieved via cosine similarity |
| KFP LocalRunner (`RUN_MODE=kfp python3 run_local.py`) | 86 chunks | 86 vectors | 5 chunks retrieved |

### Design Decisions Made for the POC

| Decision | Choice | Rationale |
|---|---|---|
| Output format from Step 1 | Parquet | Feast-native; schema: `chunk_id, document_id, file_name, chunk_index, chunk_text, vector, created` |
| Embedding location | Step 1 (inside RayJob) | Single distributed pass; avoids reloading the model in a separate step |
| Milvus deployment | Milvus Lite (file-based) | Zero infra for local testing; `data/online_store.db` |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` (384-dim) | Lightweight, runs on CPU |

### Lessons Learned

1. **`feast apply` must receive explicit definitions.** Calling `store.apply(objects=[], partial=False)` wipes all registered feature views and entities. Always import and pass the definitions:
   ```python
   from definitions import chunk, rag_feature_view
   store.apply([chunk, rag_feature_view])
   ```

2. **KFP 2.16 API change.** The `LocalRunner` class no longer exists. Use:
   ```python
   from kfp.local import SubprocessRunner, init
   init(runner=SubprocessRunner(use_venv=False))
   result = rag_pipeline(...)  # call pipeline function directly
   ```

3. **`SubprocessRunner(use_venv=False)` is essential for local testing.** With `use_venv=True` (default), KFP creates a fresh venv and installs all `packages_to_install` for every component execution — ray, docling, sentence-transformers, torch, etc. This takes many minutes per step. `use_venv=False` reuses the current environment.

4. **Subprocess isolation (multiprocessing.Process) does not work inside KFP components on macOS.** The chain KFP SubprocessRunner → Ray actor → `fork()` crashes because torch/numpy are not fork-safe. The fix: use inline processing (Docling runs directly in the Ray actor) for the KFP component. The standalone `step1_process.py` retains subprocess isolation since it doesn't have the extra nesting. On Linux/RHOAI with the `spawn` method this may also hit pickling issues with nested functions — test on-cluster before assuming it works.

5. **`python3 -m pip install` vs `pip3 install`.** On macOS, `pip3` can point to a different Python version than `python3`. Always use `python3 -m pip install` to ensure packages land in the correct interpreter.

6. **Feast's `retrieve_online_documents_v2` returns distance, not similarity.** Lower distance = more similar. The `distance_metric="COSINE"` parameter configures the metric at query time.

7. **Docling imports must stay inside the actor class**, not at module top level. Importing Docling at the top of a Ray actor file causes serialization issues because Docling loads heavy native extensions. Import inside `__init__` or `__call__`.

---

## Next Steps

1. ~~Build and test local POC~~ — Done (2026-04-21)
2. Set up remaining RHOAI prerequisites (Milvus, MinIO, Feast, RBAC)
3. Adapt `pipeline.py` for RHOAI: S3 paths, Milvus endpoint, CodeFlare SDK for RayJob
4. End-to-end test on the ana-rosa5 cluster
5. Revisit Option 1 (pure Feast) once the two-stage pipeline is proven on-cluster

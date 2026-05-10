# Real-Time Document Processing with Ray and Docling

This POC explores approaches for processing PDF documents **on demand** using
[Ray](https://www.ray.io/) and [Docling](https://github.com/DS4SD/docling) on
Red Hat OpenShift AI (RHOAI).

The goal is to find the right pattern for real-time document processing — where
a user triggers processing of individual documents rather than batching thousands
at once.

## Context

The batch processing pattern (Ray Data + ActorPoolStrategy) works well for bulk
ingestion but introduces high latency for individual documents due to setup
overhead. This POC tests alternatives and measures their trade-offs.

## Approaches Under Evaluation

| Option | Approach | Status |
|---|---|---|
| **A** | One RayJob per document via Job Client | Tested (10 + 50 docs) |
| **B** | `ray.init()` + remote actors (warm actors) | Recommended for POC, not yet tested |
| **C** | Ray Serve (HTTP endpoint with warm replicas) | Local prototype tested |

See `real-time-processing-options.md` for detailed comparison of all three options.

## Directory Structure

```
real-time-data-processing/
├── README.md                         # This file
├── real-time-processing-options.md   # Comparison of all three approaches (A–C)
├── issues_to_report.md               # Issues found for upstream repos
├── option-a/                         # Per-Document RayJobs (RHOAI cluster)
│   ├── ray-cluster-docling-per-doc.ipynb  # Main notebook
│   ├── ray_single_doc_process.py          # Per-document processor
│   ├── ray_data_process.py                # Batch processor (baseline comparison)
│   └── per-doc-processing-design.md       # Design, test results, findings
└── option-c/                         # Ray Serve (local + cluster)
    ├── serve_app.py                       # Serve deployment module (portable to RHOAI)
    ├── run_serve_local.py                 # Local launcher
    └── serve_client.py                    # Test client with performance reporting
```

## Option A Results (Per-Document RayJobs)

| Metric | 10 docs | 50 docs |
|---|---|---|
| Succeeded | 10/10 | 50/50 |
| Wall clock | 119.6s | 369.7s |
| Throughput | 0.08 docs/sec | 0.14 docs/sec |
| Avg per-job | 64.7s | 200.3s |
| First-doc latency | ~20.9s | ~28.5s |

Key finding: each per-document job incurs significant overhead from cold Docling
initialization (~26s overhead on a 1-page doc). See
`option-a/per-doc-processing-design.md` for full analysis.

## Option C Results (Ray Serve, Local)

| Metric | Option C | Option A (10 docs) |
|---|---|---|
| Wall clock | 8.7s | 119.6s |
| Throughput | 0.344 docs/sec | 0.08 docs/sec |
| First-doc latency | 7.4s | 20.9s |
| Warm-doc latency | 0.5–0.8s | N/A (cold every job) |

Key finding: warm Serve replicas eliminate per-document Docling init entirely.
After the first request, subsequent documents complete in under 1 second.

## Cluster Configuration (Option A)

| Setting | Value | Reason |
|---|---|---|
| `enableInTreeAutoscaling` | `false` (SDK default) | `true` causes autoscaler CrashLoopBackOff |
| Head `num-cpus` | `2` | Allows job driver scheduling; `0` blocks Ray Data |
| Worker `num-cpus` | `WORKER_CPUS - 2` | Reserves CPUs for Ray system processes |

The notebook applies this configuration automatically via a JSON patch in Step 4.

## Prerequisites

**Option A (RHOAI cluster):**
- RHOAI with KubeRay operator installed
- A ReadWriteMany PVC with input PDFs
- Container image with Ray + Docling (`quay.io/cathaloconnor/docling-ray:latest`)
- Workbench with `codeflare-sdk` installed

**Option C (local):**
- `ray[serve]`, `docling`, `orjson`, `requests` installed

## Quick Start — Option A (Per-Document RayJobs on RHOAI)

1. Clone this repo in your RHOAI workbench
2. Open `option-a/ray-cluster-docling-per-doc.ipynb`
3. Set your credentials in Step 2
4. Set `NUM_DOCS` in Step 3
5. Run all cells — the notebook creates the cluster, patches it, submits jobs, and reports results

## Quick Start — Option C (Ray Serve, Local)

```bash
# Terminal 1: Start the Serve endpoint
cd real-time-data-processing/option-c
python run_serve_local.py

# Terminal 2: Test with curl
curl -X POST http://127.0.0.1:8100/ \
  -H "Content-Type: application/json" \
  -d '{"file_path": "/path/to/document.pdf"}'

# Terminal 2: Run the full test client (uses pdfs from feast-raydata-local-poc/)
python serve_client.py

# Or specify a custom PDF directory
python serve_client.py /path/to/pdfs --concurrent
```

Environment variables for `option-c/serve_app.py`:

| Variable | Default | Description |
|---|---|---|
| `INPUT_BASE_DIR` | (empty) | Base directory for resolving relative file paths |
| `OUTPUT_BASE_DIR` | (empty) | Where to write markdown/json output (empty = skip) |
| `NUM_REPLICAS` | `1` | Number of Serve replicas |
| `NUM_CPUS` | `2` | CPUs per replica |
| `WRITE_JSON` | `true` | Whether to export JSON alongside markdown |

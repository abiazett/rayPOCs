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
| **A** (current) | One RayJob per document via Job Client | Tested (10 + 50 docs) |
| **B** | Ray Data with `batch_size=1` (warm actors) | Documented, not yet tested |
| **C** | Ray Data Streaming (persistent pipeline) | Documented, not yet tested |
| **D** | Ray Serve (HTTP endpoint with warm replicas) | Recommended by Ray docs, not yet tested |

See `real-time-processing-options.md` for detailed comparison of all four options.

## Option A Results (Per-Document RayJobs)

| Metric | 10 docs | 50 docs |
|---|---|---|
| Succeeded | 10/10 | 50/50 |
| Wall clock | 119.6s | 369.7s |
| Throughput | 0.08 docs/sec | 0.14 docs/sec |
| Avg per-job | 64.7s | 200.3s |
| First-doc latency | ~20.9s | ~28.5s |

Key finding: each per-document job incurs significant overhead from cold Docling
initialization. See `per-doc-processing-design.md` for full analysis.

## Files

| File | Purpose |
|---|---|
| `ray-cluster-docling-per-doc.ipynb` | Main notebook: create cluster, submit per-doc jobs, report |
| `ray_single_doc_process.py` | Per-document processor with subprocess isolation and timing breakdown |
| `ray_data_process.py` | Batch processor (used for baseline comparison in Step 7b) |
| `per-doc-processing-design.md` | Option A design, test results, cluster config, findings |
| `real-time-processing-options.md` | Comparison of all four approaches (A–D) |
| `issues_to_report.md` | Issues found for upstream repos (autoscaler, CodeFlare SDK) |

## Cluster Configuration

| Setting | Value | Reason |
|---|---|---|
| `enableInTreeAutoscaling` | `false` (SDK default) | `true` causes autoscaler CrashLoopBackOff |
| Head `num-cpus` | `2` | Allows job driver scheduling; `0` blocks Ray Data |
| Worker `num-cpus` | `WORKER_CPUS - 2` | Reserves CPUs for Ray system processes |

The notebook applies this configuration automatically via a JSON patch in Step 4.

## Prerequisites

- RHOAI with KubeRay operator installed
- A ReadWriteMany PVC with input PDFs
- Container image with Ray + Docling (`quay.io/cathaloconnor/docling-ray:latest`)
- Workbench with `codeflare-sdk` installed

## Quick Start

1. Clone this repo in your RHOAI workbench
2. Open `ray-cluster-docling-per-doc.ipynb`
3. Set your credentials in Step 2
4. Set `NUM_DOCS` in Step 3
5. Run all cells — the notebook creates the cluster, patches it, submits jobs, and reports results

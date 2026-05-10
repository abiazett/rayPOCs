# Real-Time Document Processing: Approach Options

This document compares approaches for processing PDF documents on-demand using Ray and Docling on a persistent RayCluster.

See also:
- `option-a/per-doc-processing-design.md` — Option A design, test results, and findings
- `issues_to_report.md` — Issues found during testing (autoscaler, CodeFlare SDK)

## Summary

| Option | Approach | Docling Init | Per-Doc Overhead | Works Today? |
|---|---|---|---|---|
| **A** (tested) | One RayJob per document | Every job (cold) | High (job lifecycle) | Yes |
| **B** | `ray.init()` + remote actors | Once per actor (warm) | Low (task scheduling) | Yes |
| **C** (tested locally) | Ray Serve (HTTP endpoint) | Once per replica (warm) | Minimal (~3ms) | Local tested; cluster needs `RayService` CRD |

**Recommendation:** Option B for POC/development, Option C for production.

## Why Ray Data Is Not the Right Tool for Real-Time Processing

Ray Data (`ray.data`) is designed for **offline batch processing** of large datasets. It is the wrong abstraction for on-demand, per-document processing for several fundamental reasons:

**1. High fixed setup cost per job.**
Every Ray Data pipeline invocation — regardless of how many documents it processes — pays a fixed overhead of ~140 seconds for `runtime_env` resolution, dataset materialization, and `ActorPoolStrategy` initialization. For a single document, this means 140 seconds of setup to process a file that takes 3 seconds to convert. This overhead cannot be amortized when documents arrive one at a time.

**2. Batch-oriented execution model.**
Ray Data processes items through a pipeline: `read_*()` → `map_batches()` → `write_*()`. The entire pipeline must be constructed, scheduled, and torn down for each invocation. There is no way to keep a Ray Data pipeline "open" and feed it individual documents as they arrive. Each new document requires a full pipeline lifecycle.

**3. No persistent state between invocations.**
Ray Data actors created via `ActorPoolStrategy` are warm only within a single pipeline run. When the pipeline finishes, the actors are destroyed. The next invocation creates new actors from scratch, re-loading Docling models (~26 seconds). In contrast, Ray Serve replicas stay alive indefinitely, loading models once at startup.

**4. Ray's own documentation is explicit.**
Ray Data is positioned for "offline batch inference" and "data preprocessing for ML training." For on-demand, real-time workloads, Ray's documentation directs users to **Ray Serve**, which is purpose-built for exactly this pattern.

**5. No request/response interface.**
Ray Data has no built-in way to accept an HTTP request and return a response. It reads from sources (files, S3, databases) and writes to sinks. Building a request/response layer on top of Ray Data would mean reinventing what Ray Serve already provides.

### What Ray Data IS good for

Ray Data excels at **bulk ingestion** — processing thousands of documents in a single pipeline run where the 140-second setup cost is amortized across all files, and `ActorPoolStrategy` distributes work efficiently across a pool of warm actors. For ingesting 10,000 PDFs into a vector database, Ray Data is the right choice. For processing a single PDF when a user clicks "upload," it is not.

## Why Ray Serve Is the Right Tool

Ray Serve is Ray's native framework for **online serving** — the exact use case of processing documents on demand. It maps directly to the requirements:

| Requirement | Ray Data | Ray Serve |
|---|---|---|
| Process one document on demand | 140s setup + 3s conversion | 3s conversion (warm replica) |
| Keep models loaded between requests | No (actors destroyed per pipeline) | Yes (replicas persist indefinitely) |
| Accept HTTP requests | No native support | Built-in HTTP router |
| Queue excess requests | No (pipeline is all-or-nothing) | Built-in per-replica request queue |
| Scale based on load | No (fixed actor pool per run) | Autoscaling based on queue depth |
| Health checks and auto-restart | No | Built-in replica health monitoring |
| First-document latency | ~28s (cold Docling init every job) | <1s (warm converter after startup) |

Ray Serve deployments are persistent Ray actors with an HTTP interface. Each replica initializes Docling's `DocumentConverter` once at startup (~10 seconds) and keeps it loaded in memory. Every subsequent request skips model loading entirely and goes straight to conversion. Our local test measured **0.5–0.8 seconds per document** after warmup, compared to **28+ seconds** per document with Ray Data/RayJobs.

The progression from batch to real-time is: **Ray Data** (bulk ingestion) → **Ray Serve** (on-demand processing). They are complementary tools for different stages of a document processing pipeline.

## Option A — Per-Document RayJobs (Tested)

Submit one RayJob per document via the Ray Job Submission Client to a persistent RayCluster. Each job runs a standalone Python script that initializes Docling, converts one PDF, and exits.

See `option-a/per-doc-processing-design.md` for full design, test results, and findings.

**Pros:**
- Simple to implement — each job is independent, no shared state
- Strong fault isolation — one failed document = one failed job, others unaffected
- Works with existing `RayJob` CRD and CodeFlare SDK
- `entrypoint_num_cpus` provides built-in concurrency control (Ray queues excess jobs)

**Cons:**
- Cold Docling init on every job — each job spins up a new driver process on the head pod
- `runtime_env` pip install on every job (cached after first, but still checked)
- Ray job lifecycle overhead (submission, worker assignment, environment setup)
- Goes through the Dashboard API for each submission, adding unnecessary overhead
- Ray Jobs are designed for self-contained batch workloads, not request/response

**Test results (50 docs):** 369.7s wall clock, 0.14 docs/sec, avg 200.3s per job. First-doc latency ~28.5s.

## Option B — `ray.init()` + Remote Actors (Recommended for POC)

A client (notebook, script, or service) connects **directly** to a long-lived cluster via `ray.init()` and submits work as remote function or actor calls. No job overhead — the connection is direct to the cluster, bypassing the Dashboard API entirely.

```python
import ray

# Connect directly to the persistent cluster
ray.init(address="ray://ray-cluster-head-svc:10001")

@ray.remote(num_cpus=2)
class DoclingProcessor:
    def __init__(self):
        from docling.document_converter import DocumentConverter
        self.converter = DocumentConverter()  # initialized ONCE, stays warm

    def process(self, file_path: str) -> dict:
        result = self.converter.convert(file_path)
        doc = result.document
        return {
            "markdown": doc.export_to_markdown(),
            "pages": len(doc.pages) if doc.pages else 0,
        }

# Create warm actors
actors = [DoclingProcessor.remote() for _ in range(NUM_ACTORS)]

# Submit documents — Ray task scheduler queues and bin-packs onto workers
futures = []
for i, pdf_path in enumerate(pdf_paths):
    actor = actors[i % len(actors)]
    futures.append(actor.process.remote(pdf_path))

# Collect results
results = ray.get(futures)
```

**Pros:**
- Warm actors — Docling models load once per actor, stay in memory across all requests
- No job overhead — skips the Dashboard API, no driver process per document
- Ray's task scheduler properly queues and bin-packs tasks onto workers
- Works today on a persistent `RayCluster` with no new CRDs or features needed
- Simple to implement from a notebook or script
- Natural concurrency control via actor count and `num_cpus`

**Cons:**
- Requires a persistent client connection to the cluster (notebook or long-running process)
- No built-in HTTP endpoint — the client must manage the `ray.init()` connection
- Fault tolerance is manual — if an actor dies, the client must handle retry/restart
- Less operational visibility compared to RayJobs (no job status in dashboard)
- Client must be in the same network as the Ray head (or use Ray Client protocol)

**When to use:** POC and development. This is the fastest path to validating real-time processing with warm actors. It eliminates the per-job overhead entirely and proves whether warm Docling actors solve the latency problem.

## Option C — Ray Serve (Recommended for Production)

Ray Serve is Ray's built-in framework for real-time, on-demand inference and processing. Each Serve **deployment replica** is a persistent Ray actor with a warm model/converter. Documents are submitted via HTTP requests to a persistent endpoint — no job submission, no client connection management.

```python
from ray import serve
from docling.document_converter import DocumentConverter

@serve.deployment(num_replicas=2, ray_actor_options={"num_cpus": 2})
class DoclingService:
    def __init__(self):
        # Initialized ONCE per replica — stays warm across all requests
        self.converter = DocumentConverter()

    async def __call__(self, request):
        doc_path = (await request.json())["path"]
        result = self.converter.convert(doc_path)
        return result.document.export_to_markdown()

app = DoclingService.bind()
```

On OpenShift, this deploys via the KubeRay `RayService` CRD, which manages the Serve application lifecycle on the cluster. The user interaction becomes:

```bash
curl -X POST https://doc-processor.apps.cluster.example.com/process \
  --data-binary @my_document.pdf
```

This could be abstracted into a simple one-liner in the CodeFlare SDK.

**Pros:**
- Warm replicas — Docling `DocumentConverter` loads models once per replica at startup, stays loaded across all requests
- Minimal per-request overhead — ~7-10ms routing overhead per request, negligible for CPU-intensive PDF conversion
- Built-in autoscaling — scales replicas based on queue depth, with `max_queued_requests` for load shedding
- Built-in request queuing — no external queue needed, Ray Serve handles queuing and scaling internally
- Fault tolerance — replicas auto-restart on failure, with health checks
- HTTP endpoint — standard request/response interface, easy to integrate with any client
- Production-ready — designed for exactly this use case (on-demand processing with warm models)
- No persistent client connection needed — any HTTP client can submit documents

**Cons:**
- **RHOAI does not officially support or test Ray Serve.** The `RayService` CRD and Serve runtime have not been validated on the product, so adopting this approach may require extended effort to mature it (testing, support gaps, potential KubeRay/ODH integration issues). This is a significant adoption risk.
- Requires a running `RayService` instead of submitting `RayJob` CRDs — different operational model
- Cluster resources are held while the service is up
- Subprocess isolation for timeout protection needs adaptation for the async Serve handler

**Path to production:** Option B validates the warm-actor pattern today. Ray Serve is the productization of that same pattern — adding HTTP routing, autoscaling, and operational tooling on top. The progression is: Option A (baseline) → Option B (POC) → Option C (production).

## Comparison

| Concern | Option A (Jobs) | Option B (Remote Actors) | Option C (Serve) |
|---|---|---|---|
| Docling init | Every job (cold) | Once per actor (warm) | Once per replica (warm) |
| Per-doc overhead | High (job lifecycle + Dashboard API) | Low (task scheduling only) | Minimal (~7-10ms) |
| Built-in autoscaling | No | No | Yes |
| Built-in request queuing | No | No | Yes |
| Fault tolerance | Per-job isolation | Manual (client handles) | Auto-restart replicas |
| API | Job submission client | `ray.init()` + actor calls | HTTP endpoint |
| Client requirement | Any (submits via API) | Must hold `ray.init()` connection | Any HTTP client |
| Works on RHOAI today | Yes | Yes | No (`RayService` not supported) |
| Ray's recommendation | Batch orchestration | Ad-hoc / development | **Real-time serving** |

## Option C Local Test Results (2026-05-10)

Tested locally on macOS with 3 PDFs (1–16 pages each), 1 Serve replica, 2 CPUs.

| Metric | Option C (Serve) | Option A (10 docs) |
|---|---|---|
| Documents | 3 | 10 |
| Wall clock | 8.7s | 119.6s |
| Throughput | 0.344 docs/sec | 0.08 docs/sec |
| First-doc latency | 7.4s | 20.9s |
| Avg latency | 2.9s | 64.7s |
| Warm-doc latency | 0.5–0.8s | N/A (cold every job) |
| Docling init | Once at replica startup (~10s) | Every job (~26s) |

**Key finding:** Warm replicas eliminate per-document Docling initialization entirely. After the first request (7.4s, which included the 16-page doc being the largest), subsequent documents complete in under 1 second. The total overhead per request is ~3ms (HTTP routing + JSON parsing).

**Files:** `option-c/serve_app.py` (deployment), `option-c/run_serve_local.py` (launcher), `option-c/serve_client.py` (test client)

## Next Steps

1. **Option C on RHOAI:** Deploy the Serve application on a persistent RayCluster using `RayService` CRD. Adapt `serve_app.py` for PVC-mounted file paths. Test with 50+ documents to compare with Option A at scale.
2. **Option B POC:** Optionally test warm actors via `ray.init()` as a simpler alternative that works without `RayService` CRD support.
3. **Assess `RayService` CRD readiness on RHOAI** — determine if KubeRay operator in RHOAI 3.0 includes the RayService CRD.

## Appendix: Other Considered Approaches

### Ray Data with `batch_size=1`

Submit a single RayJob using Ray Data with `ActorPoolStrategy` and `batch_size=1`. Actors stay warm, but each job submission still incurs ~140s of Ray Data setup overhead. Ruled out because the setup overhead negates the warm-actor benefit for individual documents.

### Ray Data Streaming

A persistent Ray Data pipeline that watches for new documents. Ray Data is explicitly documented as being for offline batch inference — streaming is not an officially supported pattern. Option B (remote actors) achieves the same warm-actor benefit with a simpler, supported API. Option C (Ray Serve) is the official version of this idea.

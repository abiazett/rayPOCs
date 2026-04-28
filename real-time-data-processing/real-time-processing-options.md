# Real-Time Document Processing: Approach Options

This document compares approaches for processing PDF documents on-demand using Ray and Docling on a persistent RayCluster.

See also:
- `per-doc-processing-design.md` — Option A design, test results, and findings
- `issues_to_report.md` — Issues found during testing (autoscaler, CodeFlare SDK)

## Summary

| Option | Approach | Docling Init | Per-Doc Overhead | Works Today? |
|---|---|---|---|---|
| **A** (tested) | One RayJob per document | Every job (cold) | High (job lifecycle) | Yes |
| **B** | `ray.init()` + remote actors | Once per actor (warm) | Low (task scheduling) | Yes |
| **C** | Ray Serve (HTTP endpoint) | Once per replica (warm) | Minimal (~7-10ms) | Needs `RayService` CRD |

**Recommendation:** Option B for POC/development, Option C for production.

## Option A — Per-Document RayJobs (Tested)

Submit one RayJob per document via the Ray Job Submission Client to a persistent RayCluster. Each job runs a standalone Python script that initializes Docling, converts one PDF, and exits.

See `per-doc-processing-design.md` for full design, test results, and findings.

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

## Next Steps

1. **Option B POC:** Connect directly to the cluster via `ray.init()`, create warm Docling actors, and measure per-document latency without job overhead. Compare against Option A results.
2. **Validate warm-actor benefit:** Confirm that warm Docling actors eliminate the per-document init overhead seen in Option A (~26s overhead on a 1-page doc).
3. **Option C exploration:** Once Option B validates the pattern, explore Ray Serve as the production path. Assess `RayService` CRD readiness on RHOAI.

## Appendix: Other Considered Approaches

### Ray Data with `batch_size=1`

Submit a single RayJob using Ray Data with `ActorPoolStrategy` and `batch_size=1`. Actors stay warm, but each job submission still incurs ~140s of Ray Data setup overhead. Ruled out because the setup overhead negates the warm-actor benefit for individual documents.

### Ray Data Streaming

A persistent Ray Data pipeline that watches for new documents. Ray Data is explicitly documented as being for offline batch inference — streaming is not an officially supported pattern. Option B (remote actors) achieves the same warm-actor benefit with a simpler, supported API. Option C (Ray Serve) is the official version of this idea.

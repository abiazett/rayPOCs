# Real-Time Document Processing: Approach Options

This document compares four approaches for processing PDF documents on-demand using Ray and Docling on a persistent RayCluster.

See also:
- `per-doc-processing-design.md` — Option A design, test results, and findings
- `issues_to_report.md` — Issues found during testing (autoscaler, CodeFlare SDK)

## Summary

| Option | Approach | Docling Init | Per-Doc Overhead | Ray's Recommendation |
|---|---|---|---|---|
| **A** (tested) | One RayJob per document | Every job (cold) | High (job lifecycle) | Batch orchestration |
| **B** | Ray Data with `batch_size=1` | Once per actor (warm) | High (Ray Data init) | Offline batch inference |
| **C** | Ray Data Streaming | Once per actor (warm) | Low (pipeline stays up) | Not a real pattern |
| **D** | Ray Serve | Once per replica (warm) | Minimal (~7-10ms) | **Real-time serving** |

## Option A — Per-Document RayJobs (Current Test)

Submit one RayJob per document via the Ray Job Submission Client to a persistent RayCluster. Each job runs a standalone Python script that initializes Docling, converts one PDF, and exits.

See `per-doc-processing-design.md` for full design, test results, and findings.

**Pros:**
- Simple to implement — each job is independent, no shared state
- Strong fault isolation — one failed document = one failed job, others unaffected
- Works with existing `RayJob` CRD and CodeFlare SDK
- `entrypoint_num_cpus` provides built-in concurrency control (Ray queues excess jobs)

**Cons:**
- Cold Docling init on every job (~41s estimated overhead per document)
- `runtime_env` pip install on every job (cached after first, but still checked)
- Ray job lifecycle overhead (submission, worker assignment, environment setup)
- Not what Ray Jobs is designed for — it's a batch orchestration tool

## Option B — Ray Data with `batch_size=1`

Submit a **single RayJob** that uses Ray Data with `ActorPoolStrategy` and `batch_size=1`. Each actor processes one file at a time, but actors stay warm across files — avoiding repeated Docling initialization.

```python
ds = ray.data.from_pandas(pd.DataFrame({"path": [single_pdf_path]}))
ds.map_batches(
    DoclingProcessor,
    compute=ray.data.ActorPoolStrategy(min_size=1, max_size=2),
    batch_size=1,
    num_cpus=CPUS_PER_ACTOR,
)
```

**Pros:**
- Warm actors — Docling `DocumentConverter` loads models once per actor, not once per document
- Uses Ray Data's built-in scheduling, fault tolerance, and actor pool management
- Can process a single document or a small batch with the same code path

**Cons:**
- Still incurs Ray Data setup overhead (~140s observed in batch test) on every job submission, which may negate the warm-actor benefit for single documents
- Ray Data is designed for dataset-scale operations — using it for one file at a time may be over-engineering
- No advantage over Option A if `runtime_env` pip install and Ray Data init dominate the overhead

**When to consider:** If the 50-doc test shows that Docling init (not pip/Ray Data setup) is the dominant overhead per job in Option A, Option B could help by keeping actors warm. But if the setup overhead is the bottleneck, Option D is the better path.

## Option C — Ray Data Streaming

A persistent Ray Data pipeline that stays running and watches for new documents, processing them as they arrive. This keeps actors warm (no repeated Docling init) **and** avoids repeated Ray Data setup overhead — the pipeline initializes once and stays up.

**Pros:**
- Warm actors + zero per-document setup overhead — the best of both worlds
- Low latency for individual documents (no job submission overhead)
- Natural fit for a real-time processing service

**Cons:**
- More complex to implement — needs a document arrival mechanism (file watcher, queue, API)
- Cluster resources are held even when idle
- Error handling and recovery are more complex in a long-running pipeline
- Ray Data is explicitly documented as being for offline batch inference — streaming is not an officially supported pattern

**When to consider:** If both Docling init and Ray Data setup are significant overhead in Options A/B, making neither viable for low-latency real-time processing. However, Option D (Ray Serve) is the officially supported version of this idea.

## Option D — Ray Serve (Recommended by Ray docs)

Ray Serve is Ray's built-in framework for real-time, on-demand inference and processing. Each Serve **deployment replica** is a persistent Ray actor with a warm model/converter. Documents are submitted via HTTP requests to a persistent endpoint — no job submission, no cold starts.

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

On OpenShift, this deploys via the KubeRay `RayService` CRD (instead of `RayJob`), which manages the Serve application lifecycle on the cluster.

**Pros:**
- Warm replicas — Docling `DocumentConverter` loads models once per replica at startup, stays loaded across all requests. This eliminates the per-document init overhead (~41s in Option A)
- Minimal per-request overhead — ~7-10ms routing overhead per request, negligible for CPU-intensive PDF conversion
- Built-in autoscaling — scales replicas based on queue depth, with `max_queued_requests` for load shedding
- Fault tolerance — replicas auto-restart on failure, with health checks
- HTTP endpoint — standard request/response interface, easy to integrate with other services
- Production-ready — designed for exactly this use case (on-demand processing with warm models)

**Cons:**
- **RHOAI does not officially support or test Ray Serve.** The `RayService` CRD and Serve runtime have not been validated on the product, so adopting this approach may require extended effort to mature it (testing, support gaps, potential KubeRay/ODH integration issues). This is a significant adoption risk.
- Requires a running `RayService` instead of submitting `RayJob` CRDs — different operational model
- Cluster resources are held while the service is up (same as Option C)
- Subprocess isolation for timeout protection needs adaptation for the async Serve handler
- Less familiar pattern for data engineering teams used to batch/job submission workflows

## Comparison

| Concern | Option A (Jobs) | Option B (Data batch=1) | Option C (Data Streaming) | Option D (Serve) |
|---|---|---|---|---|
| Docling init | Every job (cold) | Once per actor (warm) | Once per actor (warm) | Once per replica (warm) |
| Per-doc setup overhead | High (job lifecycle) | High (Ray Data init) | Low (pipeline stays up) | Minimal (~7-10ms) |
| Built-in autoscaling | No | No | No | Yes |
| Fault tolerance | Per-job retry | Ray Data error handling | Complex (long-running) | Auto-restart replicas |
| API | Job submission client | Job submission client | Custom file watcher | HTTP endpoint |
| Ray's recommendation | Batch orchestration | Offline batch inference | Not a real pattern | **Real-time serving** |

## Next Steps

1. Complete the 50-doc Option A test to quantify overhead breakdown (Docling init vs pip install vs conversion)
2. Use the timing data to decide whether to proceed with Option D (Ray Serve)
3. If Ray Serve: build a minimal deployment, deploy via `RayService` CRD, compare latency against Option A

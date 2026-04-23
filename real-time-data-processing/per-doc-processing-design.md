# Per-Document Processing with Ray Jobs on a Persistent RayCluster

## Goal

Test submitting documents **one at a time** as individual RayJobs to a persistent RayCluster, simulating a real-time environment where a user triggers processing on demand rather than batching all documents into a single job.

## How It Works

```
Persistent RayCluster (already running)
  │
  │   ┌─── Job 1: doc_a.pdf ─── subprocess (Docling) ─── MD + JSON to PVC
  │   ├─── Job 2: doc_b.pdf ─── subprocess (Docling) ─── MD + JSON to PVC
  ├───┤    ...
  │   ├─── Job N: doc_n.pdf ─── subprocess (Docling) ─── MD + JSON to PVC
  │   └─── (Ray queues excess jobs when cluster is at capacity)
  │
  └── Notebook tracks all jobs: submission time, completion time, status, logs
```

1. A **persistent RayCluster** stays up continuously (created by the batch notebook or manually)
2. The notebook submits **one RayJob per PDF** via the Ray Job Submission Client
3. Each job runs `ray_single_doc_process.py`, which:
   - Receives a single file path via the `FILE_PATH` environment variable
   - Starts a subprocess with Docling's `DocumentConverter`
   - Converts the PDF to Markdown and JSON
   - Writes output to the shared PVC
   - Has timeout protection — kills the subprocess if it hangs
4. All jobs are submitted rapidly (with a configurable delay between submissions)
5. Ray handles queuing, scheduling, and distribution across workers
6. The notebook waits for all jobs to complete and prints a performance report

## Cluster Configuration

The correct RayCluster configuration for both per-document and batch processing:

| Setting | Value | Reason |
|---|---|---|
| `enableInTreeAutoscaling` | `false` | SDK default. Setting to `true` causes autoscaler sidecar CrashLoopBackOff on fixed-size clusters |
| Head `num-cpus` | `2` | Allows job driver to schedule on head. `0` blocks Ray Data with "No available node types" error |
| Worker `num-cpus` | `WORKER_CPUS - 2` (e.g. `2`) | Reserves 2 CPUs per worker for Ray system processes (raylet, object store) |

The CodeFlare SDK does not support setting `num-cpus` in `rayStartParams`, so a JSON patch is applied after `cluster.apply()`. See `issues_to_report.md` for details.

```python
patch = [
    {
        "op": "add",
        "path": "/spec/workerGroupSpecs/0/rayStartParams/num-cpus",
        "value": str(WORKER_CPUS - 2),
    },
    {"op": "add", "path": "/spec/headGroupSpec/rayStartParams/num-cpus", "value": "2"},
]
```

## Comparison with Batch Processing

| Aspect | Batch | Per-Document |
|---|---|---|
| Job granularity | 1 job processes ALL PDFs | 1 job per PDF |
| Ray Data usage | Dataset + ActorPoolStrategy + map_batches + repartition | None — single file, direct processing |
| Actor pool | Warm actors shared across files within the job | Each job initializes Docling from scratch |
| Cluster lifecycle | Can be ephemeral (created per batch) | Must be persistent |
| Concurrency model | Ray Data distributes across actor pool | Ray schedules independent jobs across workers |
| Fault isolation | One failed file doesn't kill the job (subprocess restart) | One failed file = one failed job (others unaffected) |
| Use case | Bulk ingestion of large document sets | Real-time / on-demand processing |

## Expected Overhead Per Job

Each per-document job incurs overhead that doesn't exist in the batch approach:

1. **pip install** — `opencv-python-headless`, `pypdfium2`, `orjson` are installed via `runtime_env` on every job submission (Ray caches these after the first install, so subsequent jobs should be faster)
2. **Docling initialization** — `DocumentConverter` loads models on every job start. In the batch approach, actors keep the converter warm across files
3. **Ray job scheduling** — job submission, worker assignment, environment setup

The test will quantify this overhead by measuring total per-job duration (submit to completion) versus the actual Docling conversion time logged by the script.

## Critical: `entrypoint_num_cpus`

**Finding from first test (2026-04-23):** Submitting 50 jobs without resource limits caused all 50 to launch simultaneously, OOM-killing both workers.

By default, `client.submit_job()` sets `entrypoint_num_cpus=0`, meaning Ray places **no CPU constraint** on the job driver process. Every submitted job starts immediately regardless of available resources.

**Fix:** Set `entrypoint_num_cpus=CPUS_PER_ACTOR` (e.g. 2) in the `submit_job()` call. This makes Ray treat each job like a task that requires N CPUs. When the cluster runs out of CPUs, excess jobs are queued in `PENDING` state until a running job finishes and releases its CPUs.

```python
client.submit_job(
    entrypoint="python ray_single_doc_process.py",
    entrypoint_num_cpus=CPUS_PER_ACTOR,  # Ray queues jobs until CPUs are free
    runtime_env={...},
)
```

With 2 workers × 2 schedulable CPUs each and `entrypoint_num_cpus=2`, Ray runs ~2 jobs concurrently and queues the rest. This is the mechanism that prevents resource exhaustion and is essential for the per-document pattern.

## Test Plan

**First test: 10 documents, fire-and-forget**
- Submit all 10 jobs with 0.5s delay between submissions
- `entrypoint_num_cpus=2` to let Ray manage concurrency
- Cluster: 2 workers, 4 CPUs each (2 schedulable per worker after Ray overhead)
- Measure: per-job duration, throughput (docs/sec), timing distribution
- Scale up to 50+ once the pattern is proven

**Key questions to answer:**
1. How much overhead does per-job submission add vs batch?
2. How does Ray queue jobs when the cluster is saturated?
3. What is the per-document latency (submit to result)?
4. Does the `runtime_env` pip cache work across jobs (first job slow, subsequent faster)?

## Test Results

### Test 1: Per-Document — 10 docs (2026-04-23)

- Cluster: 2 workers, 4 CPUs each (2 schedulable), 8GB each
- `entrypoint_num_cpus=2`, `SUBMIT_DELAY=0.5s`

| Metric | Value |
|---|---|
| Documents | 10 |
| Succeeded | 10 |
| Failed | 0 |
| Submit phase | 5.4s |
| Total wall clock | 119.6s |
| Throughput | 0.08 docs/sec |
| Per-job min | 20.9s |
| Per-job max | 114.1s |
| Per-job avg | 64.7s |
| Per-job median | 78.9s |

Timing distribution:
- <30s: 1
- 30-60s: 3
- 60-120s: 6

**Observations:**
- Large variance (20.9s to 114.1s) likely due to different PDF sizes/complexity
- Later-submitted jobs include queuing time in their duration (submitted at t=0-5s, but may not start until an earlier job finishes)
- 0.08 docs/sec throughput is low — batch comparison needed to quantify overhead

### Test 2: Batch Baseline — 10 docs (2026-04-23)

- Cluster: 2 workers, 4 CPUs each (2 schedulable), 8GB each
- Head: 2 schedulable CPUs (patched from 0 after autoscaler fix)
- `entrypoint_num_cpus=2`, ActorPoolStrategy min=1/max=2, batch_size=4

| Metric | Value |
|---|---|
| Documents | 10 |
| Succeeded | 10 |
| Failed | 0 |
| Total wall clock | 237.2s |
| Ray Data execution | 96.46s |
| Throughput (files/sec) | 0.10 |
| Throughput (pages/sec) | 0.33 |
| Total pages | 32 |
| Actors used | 1 (all files on worker-92nnc) |

**Observations:**
- Total wall clock (237.2s) includes ~140s of pip install and Ray Data setup overhead
- Actual Ray Data execution time (96.46s) is the meaningful comparison point
- Only 1 actor was used despite `max_actors=2` — Ray Data likely decided 1 was sufficient for 10 files with the available resources
- All files processed on a single worker (no distribution across nodes)

### Comparison: Per-Document vs Batch (10 docs)

From the notebook's side-by-side comparison cell:

| Metric | Per-Document | Batch |
|---|---|---|
| Documents | 10 | 10 |
| Total wall clock | 119.6s | 237.2s |
| Throughput (total wall clock) | 0.08 docs/sec | 0.04 docs/sec |
| Ray Data execution time | N/A | 96.46s |
| Throughput (execution only) | N/A | 0.10 docs/sec |
| Avg per-doc latency | 64.7s | N/A (batch) |
| First-doc latency | ~20.9s | ~237s (entire batch) |
| Concurrency model | 2 concurrent jobs (queued) | 1 actor, sequential |
| Setup overhead | ~5s (pip cached after first job) | ~140s (pip + Ray Data init) |
| Docling init | Every job (cold start) | Once per actor (warm) |
| Per-doc overhead vs batch | ~41.0s | — |

**Result: Batch is 0.5x slower than per-document** (wall clock).

**Analysis:**

1. **Per-document wins on total wall clock** (119.6s vs 237.2s) — 2x faster end-to-end. The batch approach pays a heavy ~140s setup overhead (pip install + Ray Data initialization) that dominates at small scale.

2. **Batch has better pure processing throughput** (0.10 vs 0.08 docs/sec when measuring Ray Data execution only) — warm actors avoid repeated Docling initialization, saving ~41s of overhead per document.

3. **Per-document has much lower first-document latency** — the first document completes in ~20.9s. In batch mode, no document is available until the entire pipeline finishes (~237s).

4. **Per-document is the clear winner for real-time use cases** where individual document latency matters. Batch is better for bulk ingestion at larger scale where the setup cost is amortized.

5. **The crossover point** — at larger document counts (50+), the batch setup overhead becomes negligible per document, and the ~41s per-doc overhead (cold Docling init) accumulates. Option C (Ray Data streaming with warm actors) could combine the best of both.

### Issue: Head Pod Autoscaler CrashLoopBackOff (2026-04-23)

During the batch baseline test, the job appeared stuck. Investigation revealed the head pod's **autoscaler container** was in `CrashLoopBackOff` with 1401 restarts, while the `ray-head` and worker containers were healthy.

```
ray-docling-processor-head-nhmmx:
  autoscaler    — CrashLoopBackOff (1401 restarts)
  ray-head      — Running
  kube-rbac-proxy — Running
Workers         — Running (2/2)
```

**Impact:**
- Per-document jobs (10/10) succeeded despite the autoscaler crash — they use the Ray Job Client and don't depend on autoscaling
- The batch baseline job (Ray Data + ActorPoolStrategy) appeared stuck, likely because Ray Data's scheduling interacts with the autoscaler for resource management

**Root cause:** Not yet investigated. The autoscaler was enabled via the `enableInTreeAutoscaling: true` patch applied during cluster creation. With a fixed 2-worker cluster, autoscaling is not needed.

**Follow-up:**
- Investigate autoscaler crash logs (`oc logs <head-pod> -c autoscaler`)
- Consider disabling autoscaling for fixed-size clusters
- Recreate the cluster to get a clean batch baseline comparison

## Files

| File | Purpose |
|---|---|
| `ray_single_doc_process.py` | Single-doc processor with subprocess isolation |
| `ray-cluster-docling-per-doc.ipynb` | Notebook: submit jobs, monitor, report |
| `ray-cluster-docling-batch-processing.ipynb` | Original batch notebook (for comparison) |
| `ray_data_process.py` | Original batch processor (for comparison) |

## Future Options

### Option B — Ray Data with `batch_size=1`

Instead of submitting separate RayJobs, submit a **single RayJob** that uses Ray Data with `ActorPoolStrategy` and `batch_size=1`. Each actor processes one file at a time, but the actors stay warm across files — avoiding the repeated Docling initialization that dominates per-job overhead in Option A.

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

**When to consider:** If the 50-doc test shows that Docling init (not pip/Ray Data setup) is the dominant overhead per job in Option A, Option B could help by keeping actors warm. But if the setup overhead is the bottleneck, Option C is the better path.

### Option C — Ray Data Streaming

A persistent Ray Data pipeline that stays running and watches for new documents, processing them as they arrive. This keeps actors warm (no repeated Docling init) **and** avoids repeated Ray Data setup overhead — the pipeline initializes once and stays up.

**Pros:**
- Warm actors + zero per-document setup overhead — the best of both worlds
- Low latency for individual documents (no job submission overhead)
- Natural fit for a real-time processing service

**Cons:**
- More complex to implement — needs a document arrival mechanism (file watcher, queue, API)
- Cluster resources are held even when idle
- Error handling and recovery are more complex in a long-running pipeline

**When to consider:** If both Docling init and Ray Data setup are significant overhead in Options A/B, making neither viable for low-latency real-time processing.

### Option D — Ray Serve (Recommended by Ray docs)

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
- Requires a running `RayService` instead of submitting `RayJob` CRDs — different operational model
- Cluster resources are held while the service is up (same as Option C)
- Subprocess isolation for timeout protection needs adaptation for the async Serve handler
- Less familiar pattern for data engineering teams used to batch/job submission workflows

**Why Ray Serve over Options A–C:**

| Concern | Option A (Jobs) | Option B (Data batch=1) | Option C (Data Streaming) | Option D (Serve) |
|---|---|---|---|---|
| Docling init | Every job (cold) | Once per actor (warm) | Once per actor (warm) | Once per replica (warm) |
| Per-doc setup overhead | High (job lifecycle) | High (Ray Data init) | Low (pipeline stays up) | Minimal (~7-10ms) |
| Built-in autoscaling | No | No | No | Yes |
| Fault tolerance | Per-job retry | Ray Data error handling | Complex (long-running) | Auto-restart replicas |
| API | Job submission client | Job submission client | Custom file watcher | HTTP endpoint |
| Ray's recommendation | Batch orchestration | Offline batch inference | Not a real pattern | **Real-time serving** |

**When to consider:** This is the Ray-recommended approach for real-time/on-demand processing. The Option A 50-doc test with timing breakdown will quantify the per-document overhead that Serve eliminates. If Docling init is the dominant cost (expected), Serve is the clear next step.

**Test plan:**
1. Complete the 50-doc Option A test to quantify overhead breakdown
2. Build a minimal Ray Serve deployment with Docling
3. Deploy via `RayService` CRD on the existing cluster
4. Compare per-document latency: Serve vs Option A
5. Test autoscaling behavior under load

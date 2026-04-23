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

### Test 2: Batch Baseline — 10 docs

*Pending — run Step 7b in the notebook*

## Files

| File | Purpose |
|---|---|
| `ray_single_doc_process.py` | Single-doc processor with subprocess isolation |
| `ray-cluster-docling-per-doc.ipynb` | Notebook: submit jobs, monitor, report |
| `ray-cluster-docling-batch-processing.ipynb` | Original batch notebook (for comparison) |
| `ray_data_process.py` | Original batch processor (for comparison) |

## Future: Option C — Ray Data Streaming

If per-job overhead is too high, the next approach to test is a persistent Ray Data pipeline that watches for new documents and processes them as they arrive. This would keep actors warm (no repeated Docling init) while still processing documents individually as they land. This combines the low-latency of the per-doc pattern with the efficiency of warm actors from the batch pattern.

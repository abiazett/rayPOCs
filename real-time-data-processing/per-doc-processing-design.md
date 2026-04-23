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

## Test Plan

**First test: 50 documents, fire-and-forget**
- Submit all 50 jobs with 0.5s delay between submissions
- Let Ray handle scheduling and queuing
- Cluster: 2 workers, 4 CPUs each (2 schedulable per worker after Ray overhead)
- Measure: per-job duration, throughput (docs/sec), timing distribution

**Key questions to answer:**
1. How much overhead does per-job submission add vs batch?
2. How does Ray queue jobs when the cluster is saturated (50 jobs on 2 workers)?
3. What is the per-document latency (submit to result)?
4. Does the `runtime_env` pip cache work across jobs (first job slow, subsequent faster)?

## Files

| File | Purpose |
|---|---|
| `ray_single_doc_process.py` | Single-doc processor with subprocess isolation |
| `ray-cluster-docling-per-doc.ipynb` | Notebook: submit jobs, monitor, report |
| `ray-cluster-docling-batch-processing.ipynb` | Original batch notebook (for comparison) |
| `ray_data_process.py` | Original batch processor (for comparison) |

## Future: Option C — Ray Data Streaming

If per-job overhead is too high, the next approach to test is a persistent Ray Data pipeline that watches for new documents and processes them as they arrive. This would keep actors warm (no repeated Docling init) while still processing documents individually as they land. This combines the low-latency of the per-doc pattern with the efficiency of warm actors from the batch pattern.

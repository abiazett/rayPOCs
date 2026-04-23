# Issues to Report: red-hat-ai-examples ray/data/docling

Source repo: https://github.com/red-hat-data-services/red-hat-ai-examples/tree/main/examples/ray/data/docling

## Issue 1: `enableInTreeAutoscaling: true` causes CrashLoopBackOff on fixed-size clusters

### Where

`ray-cluster-docling-batch-processing.ipynb`, Step 5 — the JSON patch applied after cluster creation:

```python
patch = [
    {"op": "replace", "path": "/spec/enableInTreeAutoscaling", "value": True},
    ...
]
```

### What happens

The CodeFlare SDK creates the RayCluster with `enableInTreeAutoscaling: false` by default. The notebook then patches it to `true`. This adds a KubeRay autoscaler sidecar container to the head pod.

On a fixed-size cluster (e.g. `num_workers=2` with `minReplicas == maxReplicas`), the autoscaler sidecar enters a **CrashLoopBackOff** loop. During testing on RHOAI 3.0 (OpenShift 4.17, KubeRay operator), the autoscaler accumulated **1401+ restarts** while the `ray-head` and worker containers remained healthy:

```
ray-docling-processor-head-nhmmx:
  autoscaler     — CrashLoopBackOff (1401 restarts)
  ray-head       — Running
  kube-rbac-proxy — Running
Workers          — Running (2/2)
```

### Impact

- **Per-document RayJobs (job submission client):** Succeeded despite the crash — they don't depend on the autoscaler.
- **Batch processing (Ray Data + ActorPoolStrategy):** Appeared stuck. Ray Data's scheduling relies on the resource manager, which interacts with the autoscaler for resource advertisements. With the autoscaler crashing, the batch job could not make progress.

### Why `enableInTreeAutoscaling: true` is problematic

1. **Fixed-size clusters don't need autoscaling.** The SDK sets `minReplicas` and `maxReplicas` to the same value (`num_workers`), so there's nothing for the autoscaler to scale. It adds a sidecar container that serves no purpose.

2. **The autoscaler sidecar crashes.** On the tested KubeRay version, the autoscaler container fails to stabilize when min == max replicas, entering CrashLoopBackOff. This is either a KubeRay bug or a misconfiguration of the autoscaler for this scenario.

3. **It breaks Ray Data batch jobs.** Even though the head and workers are running fine, the crashing autoscaler disrupts resource management enough to stall Ray Data pipelines that use `ActorPoolStrategy`.

4. **It masks real issues.** With 1401+ restarts, the head pod's restart count and events are polluted, making it harder to diagnose actual problems.

### Suggested fix

Remove the `enableInTreeAutoscaling` line from the patch entirely. The SDK default of `false` is correct for the fixed-size cluster this notebook creates. If autoscaling is truly needed, the notebook should also set different `minReplicas` and `maxReplicas` values and verify the autoscaler sidecar starts cleanly.

```python
# Remove this line from the patch:
# {"op": "replace", "path": "/spec/enableInTreeAutoscaling", "value": True},
```

### Environment

- RHOAI 3.0
- OpenShift 4.17
- KubeRay operator (version shipped with RHOAI 3.0)
- CodeFlare SDK 0.27.0
- Image: `quay.io/cathaloconnor/docling-ray:latest`

---

## Issue 2: CodeFlare SDK does not expose `num-cpus` in `rayStartParams`

### Target

CodeFlare SDK (`codeflare-sdk` 0.27.0) — `ClusterConfiguration` class.

### The problem

`ClusterConfiguration` has no parameter to set `num-cpus` in `rayStartParams` for the head or worker groups. The SDK sets `dashboard-host`, `block`, and optionally `num-gpus` and `resources`, but CPU count is left entirely to Ray's auto-detection from container resource limits.

This means Ray sees **all CPUs in the container** as schedulable, which is incorrect for workloads that need to reserve CPUs for Ray system processes.

### Why `num-cpus` control is needed

**1. Workers need CPU reservation for Ray overhead.**

Each worker pod runs Ray system processes (raylet, object store, internal bookkeeping) that consume CPU. If a worker has 4 CPUs and Ray sees all 4 as schedulable, actors compete with the raylet for resources. The standard practice is to reserve 1–2 CPUs per worker:

```
Container CPUs:    4
Ray system procs: ~2 (raylet, object store)
Schedulable:       2  ← set via num-cpus in rayStartParams
```

Without this, a cluster configured for `CPUS_PER_ACTOR=2` on 4-CPU workers would schedule 2 actors per worker instead of 1, leading to CPU oversubscription, degraded performance, and potential OOM kills.

**2. Head pod needs controlled CPU visibility.**

The head pod runs GCS, the dashboard, and the job server. In many workloads, you don't want actors scheduled on the head at all (`num-cpus=0`). In others — like per-document RayJob submission — the job driver process needs at least some CPUs to schedule on the head (`num-cpus=2`), otherwise Ray reports:

```
No available node types can fulfill resource requests {'CPU': 1.0}
```

The correct `num-cpus` for the head depends on the workload pattern, so it can't simply be hardcoded.

**3. The current workaround is fragile.**

Users must apply a JSON patch after `cluster.apply()`, which:
- Creates a race condition (the cluster starts with the wrong CPU count, then gets patched)
- Triggers a pod restart (KubeRay detects the spec change and recreates pods)
- Requires knowledge of the RayCluster CRD JSON path (`/spec/headGroupSpec/rayStartParams/num-cpus`)
- Is not documented in the SDK

### Suggested fix

Add optional parameters to `ClusterConfiguration`:

```python
ClusterConfiguration(
    ...
    head_num_cpus=2,           # maps to headGroupSpec.rayStartParams.num-cpus
    worker_num_cpus=2,         # maps to workerGroupSpecs[0].rayStartParams.num-cpus
    # Or more generally:
    head_ray_start_params={"num-cpus": "2"},
    worker_ray_start_params={"num-cpus": "2"},
)
```

A general `head_ray_start_params` / `worker_ray_start_params` dict would also solve future cases where users need to set other `rayStartParams` (e.g. `object-store-memory`, `metrics-export-port`).

### Environment

- CodeFlare SDK 0.27.0
- Tested on RHOAI 3.0, OpenShift 4.17

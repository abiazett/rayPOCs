"""Test client for the Ray Serve Docling deployment.

Discovers PDFs, submits them to the Serve endpoint, and prints a
performance report comparable to Option A results.

Usage:
    python serve_client.py                           # sequential, default PDF dir
    python serve_client.py /path/to/pdfs             # sequential, custom dir
    python serve_client.py /path/to/pdfs --concurrent # parallel submission

Environment variables:
    SERVE_URL   — endpoint URL (default: http://127.0.0.1:8000)
"""

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DEFAULT_SERVE_URL = "http://127.0.0.1:8100"
DEFAULT_PDF_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "feast-raydata-local-poc", "pdfs"
)

OPTION_A_RESULTS = {
    "10_docs": {
        "wall_clock": 119.6,
        "throughput": 0.08,
        "avg_latency": 64.7,
        "first_doc": 20.9,
    },
    "50_docs": {
        "wall_clock": 369.7,
        "throughput": 0.14,
        "avg_latency": 200.3,
        "first_doc": 28.5,
    },
}


def discover_pdfs(pdf_dir: str) -> list[str]:
    if not os.path.isdir(pdf_dir):
        print(f"ERROR: PDF directory not found: {pdf_dir}")
        sys.exit(1)
    pdfs = sorted(
        os.path.join(pdf_dir, f)
        for f in os.listdir(pdf_dir)
        if f.lower().endswith(".pdf")
    )
    if not pdfs:
        print(f"ERROR: No PDF files found in {pdf_dir}")
        sys.exit(1)
    return pdfs


def wait_for_health(serve_url: str, timeout: int = 120):
    health_url = f"{serve_url}/health"
    print(f"Waiting for Serve endpoint at {health_url} ...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(health_url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                print(f"  Endpoint healthy (uptime: {data.get('uptime_s', '?')}s)")
                return
        except requests.ConnectionError:
            pass
        time.sleep(2)
    print(f"ERROR: Serve endpoint not healthy after {timeout}s")
    sys.exit(1)


def submit_document(serve_url: str, file_path: str) -> dict:
    t_start = time.time()
    try:
        r = requests.post(
            serve_url,
            json={"file_path": file_path},
            timeout=600,
        )
        result = r.json()
        result["client_duration_s"] = round(time.time() - t_start, 3)
        return result
    except Exception as e:
        return {
            "status": "error",
            "filename": os.path.basename(file_path),
            "error": str(e)[:300],
            "client_duration_s": round(time.time() - t_start, 3),
        }


def run_sequential(serve_url: str, pdfs: list[str]) -> list[dict]:
    results = []
    for i, pdf in enumerate(pdfs):
        fname = os.path.basename(pdf)
        print(f"  [{i+1}/{len(pdfs)}] {fname} ...", end=" ", flush=True)
        result = submit_document(serve_url, pdf)
        status = result.get("status", "error")
        dur = result.get("client_duration_s", 0)
        if status == "success":
            pages = result.get("pages", 0)
            print(f"{status} ({dur}s, {pages} pages)")
        else:
            print(f"{status} ({dur}s) — {result.get('error', '')[:80]}")
        results.append(result)
    return results


def run_concurrent(serve_url: str, pdfs: list[str], max_workers: int = 4) -> list[dict]:
    results = [None] * len(pdfs)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(submit_document, serve_url, pdf): i
            for i, pdf in enumerate(pdfs)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            result = future.result()
            results[idx] = result
            fname = result.get("filename", os.path.basename(pdfs[idx]))
            status = result.get("status", "error")
            dur = result.get("client_duration_s", 0)
            print(f"  [{idx+1}/{len(pdfs)}] {fname} — {status} ({dur}s)")
    return results


def print_report(results: list[dict], wall_clock: float, mode: str):
    succeeded = [r for r in results if r.get("status") == "success"]
    failed = [r for r in results if r.get("status") != "success"]

    durations = [r["client_duration_s"] for r in succeeded]
    convert_durations = [r.get("convert_duration_s", 0) for r in succeeded]
    total_pages = sum(r.get("pages", 0) for r in succeeded)

    print("\n" + "=" * 60)
    print(f"  Option C (Ray Serve) — {len(results)} docs ({mode})")
    print("=" * 60)

    print(f"\n  Documents:    {len(results)}")
    print(f"  Succeeded:    {len(succeeded)}")
    print(f"  Failed:       {len(failed)}")
    print(f"  Total pages:  {total_pages}")
    print(f"  Wall clock:   {round(wall_clock, 1)}s")

    if succeeded:
        throughput = len(succeeded) / wall_clock if wall_clock > 0 else 0
        print(f"  Throughput:   {round(throughput, 3)} docs/sec")
        print(f"\n  --- Per-Document Latency (client-side) ---")
        print(f"  Min:    {round(min(durations), 1)}s")
        print(f"  Max:    {round(max(durations), 1)}s")
        print(f"  Avg:    {round(statistics.mean(durations), 1)}s")
        print(f"  Median: {round(statistics.median(durations), 1)}s")
        print(f"  First:  {round(durations[0], 1)}s")

        if convert_durations:
            print(f"\n  --- Docling Conversion Time (server-side) ---")
            print(f"  Min:    {round(min(convert_durations), 1)}s")
            print(f"  Max:    {round(max(convert_durations), 1)}s")
            print(f"  Avg:    {round(statistics.mean(convert_durations), 1)}s")

        buckets = {"<5s": 0, "5-15s": 0, "15-30s": 0, "30-60s": 0, ">60s": 0}
        for d in durations:
            if d < 5:
                buckets["<5s"] += 1
            elif d < 15:
                buckets["5-15s"] += 1
            elif d < 30:
                buckets["15-30s"] += 1
            elif d < 60:
                buckets["30-60s"] += 1
            else:
                buckets[">60s"] += 1
        print(f"\n  --- Distribution ---")
        for label, count in buckets.items():
            print(f"  {label:>8s}: {count}")

        print(f"\n  --- Comparison vs Option A ---")
        ref = OPTION_A_RESULTS.get("10_docs", {})
        print(f"  {'Metric':<25s} {'Option C':>10s} {'Option A (10)':>14s}")
        print(f"  {'-'*25} {'-'*10} {'-'*14}")
        print(f"  {'Wall clock (s)':<25s} {round(wall_clock, 1):>10} {ref.get('wall_clock', 'N/A'):>14}")
        print(f"  {'Throughput (docs/sec)':<25s} {round(throughput, 3):>10} {ref.get('throughput', 'N/A'):>14}")
        print(f"  {'Avg latency (s)':<25s} {round(statistics.mean(durations), 1):>10} {ref.get('avg_latency', 'N/A'):>14}")
        print(f"  {'First-doc latency (s)':<25s} {round(durations[0], 1):>10} {ref.get('first_doc', 'N/A'):>14}")

    if failed:
        print(f"\n  --- Errors ---")
        for r in failed[:10]:
            print(f"  {r.get('filename', '?')}: {r.get('error', 'unknown')[:100]}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Test client for Ray Serve Docling")
    parser.add_argument("pdf_dir", nargs="?", default=DEFAULT_PDF_DIR,
                        help="Directory containing PDF files")
    parser.add_argument("--concurrent", action="store_true",
                        help="Submit documents concurrently")
    parser.add_argument("--workers", type=int, default=4,
                        help="Max concurrent workers (default: 4)")
    parser.add_argument("--url", default=None,
                        help="Serve endpoint URL")
    args = parser.parse_args()

    serve_url = args.url or os.environ.get("SERVE_URL", DEFAULT_SERVE_URL)
    pdf_dir = os.path.abspath(args.pdf_dir)

    pdfs = discover_pdfs(pdf_dir)
    print(f"\nFound {len(pdfs)} PDFs in {pdf_dir}")
    for pdf in pdfs:
        print(f"  - {os.path.basename(pdf)}")

    wait_for_health(serve_url)

    mode = "concurrent" if args.concurrent else "sequential"
    print(f"\nSubmitting {len(pdfs)} documents ({mode})...\n")

    t_start = time.time()
    if args.concurrent:
        results = run_concurrent(serve_url, pdfs, max_workers=args.workers)
    else:
        results = run_sequential(serve_url, pdfs)
    wall_clock = time.time() - t_start

    print_report(results, wall_clock, mode)


if __name__ == "__main__":
    main()

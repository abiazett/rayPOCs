"""Single-document processing script for per-document RayJob submission.

This script processes ONE PDF file, passed via the FILE_PATH environment
variable. It is submitted as a RayJob to a persistent RayCluster.

The Docling conversion runs in a subprocess for timeout protection — if
the converter hangs, the subprocess is killed and the job reports a timeout.
"""

import multiprocessing as mp
import os
import queue
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Parameters (passed as environment variables from the job submission)
# ---------------------------------------------------------------------------
FILE_PATH = os.environ.get("FILE_PATH", "")
PVC_MOUNT_PATH = os.environ.get("PVC_MOUNT_PATH", "/mnt/data")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "output")
WRITE_JSON = os.environ.get("WRITE_JSON", "1").lower() in ("1", "true", "yes")
FILE_TIMEOUT = int(os.environ.get("FILE_TIMEOUT", "600"))
CPUS_PER_ACTOR = int(os.environ.get("CPUS_PER_ACTOR", "2"))


def _mkdir(path: Path):
    subprocess.run(["mkdir", "-p", "-m", "777", str(path)], check=False)


def _write(path, data, retries=3, delay=0.5):
    for attempt in range(retries):
        try:
            with open(path, "wb") as f:
                f.write(data)
            return
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


# ---------------------------------------------------------------------------
# Converter subprocess
# ---------------------------------------------------------------------------


def _converter_worker(req_q, res_q, cpus, output_base_str, write_json):
    """Subprocess that owns the DocumentConverter.

    Initialises Docling once, converts the file, writes output, and exits.
    Running in a subprocess means we can hard-kill it on timeout.
    """
    os.environ["OMP_NUM_THREADS"] = str(cpus)
    os.environ["MKL_NUM_THREADS"] = str(cpus)

    import io

    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorOptions,
        PdfPipelineOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=cpus,
        device="cpu",
    )
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    output_base = Path(output_base_str)
    markdown_dir = output_base / "markdown"
    json_dir = output_base / "json" if write_json else None

    if write_json:
        import orjson

    # Signal parent that initialisation is complete
    res_q.put(("ready",))

    # Process the single file
    msg = req_q.get()
    if msg is None:
        return

    file_path = msg
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        file_size = len(file_bytes)
        if file_size == 0:
            res_q.put(("error", 0, 0, 0.0, 0.0, "File empty"))
            return

        fname = os.path.basename(file_path)
        fname_base = fname.rsplit(".", 1)[0]

        stream = DocumentStream(name=fname, stream=io.BytesIO(file_bytes))
        result = converter.convert(stream)
        doc = result.document

        pages = getattr(doc, "pages", None)
        page_count = len(pages) if pages is not None else 0

        md_bytes = doc.export_to_markdown().encode("utf-8")
        md_kb = round(len(md_bytes) / 1024, 2)
        _write(markdown_dir / f"{fname_base}.md", md_bytes)

        js_kb = 0.0
        if write_json and json_dir is not None:
            json_bytes = orjson.dumps(doc.export_to_dict())
            js_kb = round(len(json_bytes) / 1024, 2)
            _write(json_dir / f"{fname_base}.json", json_bytes)

        res_q.put(("success", page_count, file_size, md_kb, js_kb, ""))

    except Exception as e:
        res_q.put(("error", 0, 0, 0.0, 0.0, str(e)[:150]))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def process_single_document():
    """Process a single PDF using subprocess isolation."""
    if not FILE_PATH:
        raise ValueError("FILE_PATH environment variable is required")

    full_path = os.path.join(PVC_MOUNT_PATH, FILE_PATH)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"File not found: {full_path}")

    fname = os.path.basename(full_path)
    output_base = Path(PVC_MOUNT_PATH) / OUTPUT_PATH
    _mkdir(output_base)
    _mkdir(output_base / "markdown")
    if WRITE_JSON:
        _mkdir(output_base / "json")

    print(f"Processing: {fname}")
    print(f"  Input:   {full_path}")
    print(f"  Output:  {output_base}")
    print(f"  Timeout: {FILE_TIMEOUT}s")

    # Start converter subprocess
    req_q = mp.Queue()
    res_q = mp.Queue()
    worker = mp.Process(
        target=_converter_worker,
        args=(req_q, res_q, CPUS_PER_ACTOR, str(output_base), WRITE_JSON),
        daemon=True,
    )
    worker.start()

    # Wait for converter to initialise
    try:
        msg = res_q.get(timeout=300)
        assert msg[0] == "ready"
    except (queue.Empty, AssertionError):
        worker.kill()
        raise RuntimeError("Converter subprocess failed to initialise")

    # Send the file for processing
    t0 = time.time()
    req_q.put(full_path)

    try:
        result = res_q.get(timeout=FILE_TIMEOUT)
        status, page_count, file_size, md_kb, js_kb, error_msg = result
        duration = round(time.time() - t0, 3)

        if status == "success":
            file_size_mb = round(file_size / (1024 * 1024), 3)
            pps = round(page_count / duration, 2) if duration > 0 else 0.0
            print(f"\n  Status:    SUCCESS")
            print(f"  Pages:     {page_count}")
            print(f"  File size: {file_size_mb} MB")
            print(f"  Duration:  {duration}s")
            print(f"  Pages/sec: {pps}")
            print(f"  Output MD: {md_kb} KB")
            if WRITE_JSON:
                print(f"  Output JSON: {js_kb} KB")
        else:
            print(f"\n  Status: ERROR")
            print(f"  Error:  {error_msg}")

    except queue.Empty:
        duration = round(time.time() - t0, 3)
        print(f"\n  Status:   TIMEOUT after {duration}s")
        worker.terminate()
        worker.join(timeout=5)
        if worker.is_alive():
            worker.kill()
            worker.join()
        raise TimeoutError(f"File {fname} timed out after {FILE_TIMEOUT}s")

    # Clean shutdown
    req_q.put(None)
    worker.join(timeout=10)
    if worker.is_alive():
        worker.kill()
        worker.join()

    print(f"\nDone: {fname} in {duration}s")


if __name__ == "__main__":
    process_single_document()

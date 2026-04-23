"""Step 1: Parse PDFs with RayData + Docling, chunk, embed, output Parquet.

Runs locally with ray.init(). Uses the same patterns as the RHOAI RayJob
(ActorPoolStrategy, subprocess isolation) but without Kubernetes.
"""

import glob
import hashlib
import io
import multiprocessing as mp
import os
import queue
import time

import pandas as pd
import ray

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
PDF_DIR = os.environ.get("PDF_DIR", "pdfs")
OUTPUT_PARQUET = os.environ.get("OUTPUT_PARQUET", "feature_repo/data/processed_chunks.parquet")
NUM_FILES = int(os.environ.get("NUM_FILES", "0"))  # 0 = all
CHUNK_MAX_TOKENS = int(os.environ.get("CHUNK_MAX_TOKENS", "256"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
CPUS_PER_ACTOR = int(os.environ.get("CPUS_PER_ACTOR", "2"))
MIN_ACTORS = int(os.environ.get("MIN_ACTORS", "1"))
MAX_ACTORS = int(os.environ.get("MAX_ACTORS", "2"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2"))
FILE_TIMEOUT = int(os.environ.get("FILE_TIMEOUT", "300"))


def _converter_worker(req_q, res_q, cpus_per_actor, chunk_max_tokens, tokenizer_name, embed_model_name):
    """Subprocess that initializes Docling + chunker + embedder once, then processes files."""
    os.environ["OMP_NUM_THREADS"] = str(cpus_per_actor)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    from docling.chunking import HybridChunker
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import AcceleratorOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from sentence_transformers import SentenceTransformer

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=cpus_per_actor, device="cpu",
    )
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )
    chunker = HybridChunker(tokenizer=tokenizer_name, max_tokens=chunk_max_tokens)
    embedder = SentenceTransformer(embed_model_name)

    res_q.put(("ready",))

    while True:
        msg = req_q.get()
        if msg is None:
            break
        file_path = msg
        fname = os.path.basename(file_path)
        stem = os.path.splitext(fname)[0]

        try:
            t_start = time.time()
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            if len(file_bytes) == 0:
                res_q.put(("error", file_path, []))
                continue

            stream = DocumentStream(name=fname, stream=io.BytesIO(file_bytes))
            result = converter.convert(stream)
            doc = result.document
            chunks = list(chunker.chunk(doc))

            rows = []
            for idx, chunk in enumerate(chunks):
                text = chunk.text.strip()
                if not text:
                    continue
                embedding = embedder.encode([text], normalize_embeddings=True).tolist()[0]
                chunk_id = hashlib.sha256(f"{fname}-{idx}-{text[:50]}".encode()).hexdigest()[:16]
                rows.append({
                    "chunk_id": chunk_id,
                    "document_id": stem,
                    "file_name": fname,
                    "chunk_index": idx,
                    "chunk_text": text,
                    "vector": embedding,
                })

            elapsed = round(time.time() - t_start, 2)
            print(f"  {fname}: {len(rows)} chunks in {elapsed}s")
            res_q.put(("success", file_path, rows))

        except Exception as e:
            print(f"  {fname}: ERROR - {str(e)[:100]}")
            res_q.put(("error", file_path, []))


class DoclingProcessor:
    """Ray Data actor that delegates to a Docling subprocess."""

    def __init__(self):
        self._start_worker()

    def _start_worker(self):
        self._req_q = mp.Queue()
        self._res_q = mp.Queue()
        self._worker = mp.Process(
            target=_converter_worker,
            args=(
                self._req_q, self._res_q, CPUS_PER_ACTOR,
                CHUNK_MAX_TOKENS, EMBED_MODEL, EMBED_MODEL,
            ),
            daemon=True,
        )
        self._worker.start()
        msg = self._res_q.get(timeout=300)
        assert msg[0] == "ready"

    def _restart_worker(self):
        if self._worker.is_alive():
            self._worker.terminate()
            self._worker.join(timeout=5)
            if self._worker.is_alive():
                self._worker.kill()
                self._worker.join()
        while True:
            try:
                self._res_q.get_nowait()
            except queue.Empty:
                break
        self._start_worker()

    def __call__(self, batch):
        all_rows = []
        for file_path in batch["path"]:
            self._req_q.put(str(file_path))
            try:
                result = self._res_q.get(timeout=FILE_TIMEOUT)
                _, _, rows = result
                all_rows.extend(rows)
            except queue.Empty:
                print(f"  TIMEOUT: {file_path}, restarting worker")
                self._restart_worker()

        if not all_rows:
            return {
                "chunk_id": [], "document_id": [], "file_name": [],
                "chunk_index": [], "chunk_text": [], "vector": [],
            }

        return {k: [r[k] for r in all_rows] for k in all_rows[0]}


def run(pdf_dir: str = PDF_DIR, output_parquet: str = OUTPUT_PARQUET) -> str:
    """Process PDFs and write Feast-compatible Parquet."""
    print("=" * 60)
    print("STEP 1: RayData + Docling — Parse, Chunk, Embed")
    print("=" * 60)

    pdf_paths = sorted(glob.glob(f"{pdf_dir}/**/*.pdf", recursive=True))
    if not pdf_paths:
        pdf_paths = sorted(glob.glob(f"{pdf_dir}/*.pdf"))
    print(f"Found {len(pdf_paths)} PDFs in {pdf_dir}")

    if NUM_FILES > 0:
        pdf_paths = pdf_paths[:NUM_FILES]
        print(f"Limited to {NUM_FILES} files")

    if not pdf_paths:
        raise FileNotFoundError(f"No PDFs found in {pdf_dir}")

    ray.init(ignore_reinit_error=True)

    ds = ray.data.from_pandas(pd.DataFrame({"path": pdf_paths}))
    target_blocks = max(MAX_ACTORS * 2, len(pdf_paths))
    ds = ds.repartition(min(target_blocks, len(pdf_paths)))

    results_ds = ds.map_batches(
        DoclingProcessor,
        compute=ray.data.ActorPoolStrategy(
            min_size=MIN_ACTORS, max_size=MAX_ACTORS,
        ),
        batch_size=BATCH_SIZE,
        batch_format="numpy",
        num_cpus=CPUS_PER_ACTOR,
    )

    # Collect results
    start_time = time.time()
    all_rows = []
    for batch in results_ds.iter_batches(batch_size=100, batch_format="numpy"):
        n = len(batch["chunk_id"])
        for i in range(n):
            all_rows.append({k: batch[k][i] for k in batch})

    wall_clock = time.time() - start_time

    if not all_rows:
        raise RuntimeError("No chunks produced from any PDF")

    df = pd.DataFrame(all_rows)
    # Convert numpy arrays to lists for vector column
    df["vector"] = df["vector"].apply(lambda x: x.tolist() if hasattr(x, "tolist") else x)
    df["created"] = pd.Timestamp.now()

    os.makedirs(os.path.dirname(output_parquet), exist_ok=True)
    df.to_parquet(output_parquet, index=False)

    print(f"\nProcessed {len(pdf_paths)} PDFs -> {len(df)} chunks")
    print(f"Wall clock: {wall_clock:.1f}s")
    print(f"Output: {output_parquet}")
    print(f"Columns: {list(df.columns)}")
    print("=" * 60)

    ray.shutdown()
    return output_parquet


if __name__ == "__main__":
    run()

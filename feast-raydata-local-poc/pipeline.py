"""KFP Pipeline: Feast + RayData + Docling RAG (runs locally or on RHOAI).

Three-step pipeline:
1. Parse & chunk PDFs with RayData + Docling -> Parquet
2. Materialize to Milvus via Feast
3. Query the RAG system

Use kfp.local.LocalRunner to execute locally without Kubernetes.
"""

from kfp import dsl


@dsl.component(
    packages_to_install=[
        "ray[data]>=2.44.0",
        "docling>=2.0.0",
        "sentence-transformers>=2.2.0",
        "pandas>=2.0.0",
        "pyarrow>=14.0.0",
    ],
)
def parse_and_chunk(
    pdf_dir: str,
    output_parquet: str,
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    chunk_max_tokens: int = 256,
    num_files: int = 0,
    max_actors: int = 2,
    cpus_per_actor: int = 2,
) -> str:
    """Parse PDFs with RayData + Docling, chunk, embed, write Parquet."""
    import glob
    import hashlib
    import io
    import os
    import time

    import pandas as pd
    import ray

    class DoclingProcessor:
        def __init__(self):
            os.environ["OMP_NUM_THREADS"] = str(cpus_per_actor)
            os.environ["TOKENIZERS_PARALLELISM"] = "false"

            from docling.chunking import HybridChunker
            from docling.datamodel.pipeline_options import (
                AcceleratorOptions,
                PdfPipelineOptions,
            )
            from docling.document_converter import DocumentConverter
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from sentence_transformers import SentenceTransformer

            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = True
            opts.accelerator_options = AcceleratorOptions(
                num_threads=cpus_per_actor, device="cpu"
            )
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: __import__(
                        "docling.document_converter", fromlist=["PdfFormatOption"]
                    ).PdfFormatOption(pipeline_options=opts)
                }
            )
            self._chunker = HybridChunker(
                tokenizer=embed_model, max_tokens=chunk_max_tokens
            )
            self._embedder = SentenceTransformer(embed_model)

        def __call__(self, batch):
            from docling.datamodel.base_models import DocumentStream

            all_rows = []
            for fp in batch["path"]:
                file_path = str(fp)
                fname = os.path.basename(file_path)
                stem = os.path.splitext(fname)[0]
                try:
                    t0 = time.time()
                    with open(file_path, "rb") as f:
                        data = f.read()
                    if not data:
                        continue
                    stream = DocumentStream(
                        name=fname, stream=io.BytesIO(data)
                    )
                    result = self._converter.convert(stream)
                    chunks = list(self._chunker.chunk(result.document))
                    for idx, chunk in enumerate(chunks):
                        text = chunk.text.strip()
                        if not text:
                            continue
                        emb = self._embedder.encode(
                            [text], normalize_embeddings=True
                        ).tolist()[0]
                        cid = hashlib.sha256(
                            f"{fname}-{idx}-{text[:50]}".encode()
                        ).hexdigest()[:16]
                        all_rows.append({
                            "chunk_id": cid,
                            "document_id": stem,
                            "file_name": fname,
                            "chunk_index": idx,
                            "chunk_text": text,
                            "vector": emb,
                        })
                    print(
                        f"  {fname}: {len([r for r in all_rows if r['file_name'] == fname])} "
                        f"chunks in {time.time()-t0:.1f}s"
                    )
                except Exception as e:
                    print(f"  {fname}: ERROR - {str(e)[:100]}")
            if not all_rows:
                return {
                    "chunk_id": [], "document_id": [], "file_name": [],
                    "chunk_index": [], "chunk_text": [], "vector": [],
                }
            return {k: [r[k] for r in all_rows] for k in all_rows[0]}

    pdf_paths = sorted(glob.glob(f"{pdf_dir}/**/*.pdf", recursive=True))
    if not pdf_paths:
        pdf_paths = sorted(glob.glob(f"{pdf_dir}/*.pdf"))
    if num_files > 0:
        pdf_paths = pdf_paths[:num_files]
    if not pdf_paths:
        raise FileNotFoundError(f"No PDFs in {pdf_dir}")

    print(f"Processing {len(pdf_paths)} PDFs...")
    ray.init(ignore_reinit_error=True)
    ds = ray.data.from_pandas(pd.DataFrame({"path": pdf_paths}))
    ds = ds.repartition(min(max_actors * 2, len(pdf_paths)))

    results_ds = ds.map_batches(
        DoclingProcessor,
        compute=ray.data.ActorPoolStrategy(min_size=1, max_size=max_actors),
        batch_size=2,
        batch_format="numpy",
        num_cpus=cpus_per_actor,
    )

    all_rows = []
    for batch in results_ds.iter_batches(batch_size=100, batch_format="numpy"):
        for i in range(len(batch["chunk_id"])):
            all_rows.append({k: batch[k][i] for k in batch})

    ray.shutdown()

    if not all_rows:
        raise RuntimeError("No chunks produced")

    df = pd.DataFrame(all_rows)
    df["vector"] = df["vector"].apply(lambda x: x.tolist() if hasattr(x, "tolist") else x)
    df["created"] = pd.Timestamp.now()

    os.makedirs(os.path.dirname(output_parquet), exist_ok=True)
    df.to_parquet(output_parquet, index=False)
    print(f"Wrote {len(df)} chunks to {output_parquet}")

    return output_parquet


@dsl.component(
    packages_to_install=[
        "feast[milvus]>=0.41.0",
        "pandas>=2.0.0",
        "pyarrow>=14.0.0",
    ],
)
def feast_materialize(
    parquet_path: str,
    repo_path: str = "feature_repo",
) -> str:
    """Materialize processed chunks to Milvus via Feast."""
    import os

    import pandas as pd
    from feast import FeatureStore

    if not os.path.exists(parquet_path):
        raise FileNotFoundError(f"Parquet not found: {parquet_path}")

    df = pd.read_parquet(parquet_path)
    if "created" not in df.columns:
        df["created"] = pd.Timestamp.now()

    print(f"Read {len(df)} chunks from {parquet_path}")

    import sys
    sys.path.insert(0, os.path.abspath(repo_path))
    from definitions import chunk, rag_feature_view

    store = FeatureStore(repo_path=repo_path)
    store.apply([chunk, rag_feature_view])
    print("Feast definitions applied.")

    store.write_to_online_store(feature_view_name="rag_documents", df=df)
    print(f"Materialized {len(df)} vectors to Milvus.")

    return f"rag_documents:{len(df)}"


@dsl.component(
    packages_to_install=[
        "feast[milvus]>=0.41.0",
        "sentence-transformers>=2.2.0",
        "pandas>=2.0.0",
    ],
)
def rag_query(
    question: str,
    repo_path: str = "feature_repo",
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    top_k: int = 5,
) -> str:
    """Query the RAG system using Feast vector search."""
    from feast import FeatureStore
    from sentence_transformers import SentenceTransformer

    store = FeatureStore(repo_path=repo_path)
    model = SentenceTransformer(embed_model)
    query_embedding = model.encode([question], normalize_embeddings=True).tolist()[0]

    context_data = store.retrieve_online_documents_v2(
        features=[
            "rag_documents:vector",
            "rag_documents:document_id",
            "rag_documents:file_name",
            "rag_documents:chunk_index",
            "rag_documents:chunk_text",
        ],
        query=query_embedding,
        top_k=top_k,
        distance_metric="COSINE",
    ).to_df()

    print(f"Question: {question}\n")
    print(f"Retrieved {len(context_data)} chunks:")
    for _, row in context_data.iterrows():
        print(f"  - {row.get('file_name', '?')} "
              f"(chunk {row.get('chunk_index', '?')}, "
              f"sim: {row.get('distance', 0):.3f})")
        print(f"    {str(row.get('chunk_text', ''))[:200]}...")
    print()

    return context_data.to_json()


@dsl.pipeline(
    name="Feast RayData Docling RAG Pipeline",
    description="Local RAG pipeline: RayData+Docling parse -> Feast materialize -> RAG query",
)
def rag_pipeline(
    pdf_dir: str = "pdfs",
    output_parquet: str = "feature_repo/data/processed_chunks.parquet",
    repo_path: str = "feature_repo",
    question: str = "What are the main topics covered in the documents?",
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    chunk_max_tokens: int = 256,
    num_files: int = 0,
    max_actors: int = 2,
    top_k: int = 5,
):
    # Step 1: Parse, chunk, embed with RayData + Docling
    step1 = parse_and_chunk(
        pdf_dir=pdf_dir,
        output_parquet=output_parquet,
        embed_model=embed_model,
        chunk_max_tokens=chunk_max_tokens,
        num_files=num_files,
        max_actors=max_actors,
    )

    # Step 2: Materialize to Milvus via Feast
    step2 = feast_materialize(
        parquet_path=step1.output,
        repo_path=repo_path,
    )

    # Step 3: Query
    step3 = rag_query(
        question=question,
        repo_path=repo_path,
        embed_model=embed_model,
        top_k=top_k,
    )
    step3.after(step2)


if __name__ == "__main__":
    from kfp import compiler

    compiler.Compiler().compile(
        rag_pipeline,
        package_path="rag_pipeline.yaml",
    )
    print("Compiled to rag_pipeline.yaml")

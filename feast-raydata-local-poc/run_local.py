"""Run the full RAG pipeline locally using KFP LocalRunner.

Usage:
    python run_local.py

    # Or run steps individually:
    python step1_process.py
    python step2_materialize.py
    python step3_query.py "Your question here"
"""

import os
import sys


def run_with_kfp_local():
    """Execute the pipeline using KFP's local execution."""
    from kfp.local import SubprocessRunner, init

    from pipeline import rag_pipeline

    init(runner=SubprocessRunner(use_venv=False))

    result = rag_pipeline(
        pdf_dir=os.path.abspath("pdfs"),
        output_parquet=os.path.abspath("feature_repo/data/processed_chunks.parquet"),
        repo_path=os.path.abspath("feature_repo"),
        question="What are the main topics covered in the documents?",
        num_files=0,
        max_actors=2,
        top_k=5,
    )
    print(f"\nPipeline result: {result}")


def run_sequential():
    """Execute steps sequentially without KFP (simpler, fewer dependencies)."""
    from step1_process import run as step1
    from step2_materialize import run as step2
    from step3_query import run as step3

    print("\n" + "=" * 60)
    print("FEAST + RAYDATA + DOCLING — LOCAL RAG PIPELINE")
    print("=" * 60 + "\n")

    # Step 1
    parquet_path = step1()

    # Step 2
    step2(parquet_path=parquet_path)

    # Step 3
    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else \
        "What are the main topics covered in the documents?"
    step3(question=question)


if __name__ == "__main__":
    mode = os.environ.get("RUN_MODE", "sequential")

    if mode == "kfp":
        print("Running with KFP LocalRunner...")
        run_with_kfp_local()
    else:
        print("Running steps sequentially...")
        run_sequential()

"""Step 2: Register Feast features and materialize to Milvus Lite."""

import os

import pandas as pd


def run(
    repo_path: str = "feature_repo",
    parquet_path: str = "feature_repo/data/processed_chunks.parquet",
) -> str:
    """Apply Feast definitions and write chunks to Milvus online store."""
    from feast import FeatureStore

    print("=" * 60)
    print("STEP 2: Feast — Materialize to Milvus")
    print("=" * 60)

    if not os.path.exists(parquet_path):
        raise FileNotFoundError(f"Parquet not found: {parquet_path}. Run step1 first.")

    df = pd.read_parquet(parquet_path)
    print(f"Read {len(df)} chunks from {parquet_path}")

    # Ensure created column is a proper timestamp
    if "created" not in df.columns:
        df["created"] = pd.Timestamp.now()

    import sys

    # Add feature_repo to path so we can import definitions
    sys.path.insert(0, os.path.abspath(repo_path))
    from definitions import chunk, rag_feature_view

    store = FeatureStore(repo_path=repo_path)

    # Apply feature definitions (registers entities + feature views, creates Milvus collection)
    print("Running feast apply...")
    store.apply([chunk, rag_feature_view])
    print("Feature definitions applied.")

    # Write to online store (Milvus Lite)
    print(f"Writing {len(df)} chunks to Milvus online store...")
    store.write_to_online_store(
        feature_view_name="rag_documents",
        df=df,
    )

    print(f"\nMaterialization complete: {len(df)} vectors in Milvus Lite")
    print(f"Online store: {repo_path}/data/online_store.db")
    print("=" * 60)

    return f"rag_documents:{len(df)}"


if __name__ == "__main__":
    run()

"""Feast feature definitions for the RayData + Docling RAG pipeline."""

from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource
from feast.data_format import ParquetFormat
from feast.types import Array, Float64, Int64, String, ValueType

chunk = Entity(
    name="chunk_id",
    description="Unique chunk identifier",
    value_type=ValueType.STRING,
    join_keys=["chunk_id"],
)

source = FileSource(
    file_format=ParquetFormat(),
    path="data/processed_chunks.parquet",
    timestamp_field="created",
)

rag_feature_view = FeatureView(
    name="rag_documents",
    entities=[chunk],
    schema=[
        Field(name="chunk_id", dtype=String),
        Field(name="document_id", dtype=String),
        Field(name="file_name", dtype=String),
        Field(name="chunk_index", dtype=Int64),
        Field(name="chunk_text", dtype=String),
        Field(
            name="vector",
            dtype=Array(Float64),
            vector_index=True,
            vector_search_metric="COSINE",
        ),
    ],
    source=source,
    ttl=timedelta(hours=24),
)

"""Step 3: Query the RAG system using Feast vector search."""

import os
import sys

from sentence_transformers import SentenceTransformer


EMBED_MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def embed_text(model, text: str) -> list[float]:
    """Generate an embedding for a query."""
    return model.encode([text], normalize_embeddings=True).tolist()[0]


def search_feast(store, query_embedding, top_k=5):
    """Search Milvus via Feast for similar chunks."""
    context_data = store.retrieve_online_documents_v2(
        features=[
            "rag_documents:vector",
            "rag_documents:document_id",
            "rag_documents:file_name",
            "rag_documents:chunk_index",
            "rag_documents:chunk_text",
            "rag_documents:chunk_id",
        ],
        query=query_embedding,
        top_k=top_k,
        distance_metric="COSINE",
    ).to_df()
    return context_data


def build_prompt(question, context_df):
    """Build a RAG prompt from retrieved chunks."""
    context_block = ""
    for i, row in context_df.iterrows():
        source = row.get("file_name", "unknown")
        chunk_idx = row.get("chunk_index", "?")
        text = row.get("chunk_text", "")
        distance = row.get("distance", 0)
        context_block += (
            f"[Source: {source}, chunk {chunk_idx}, similarity: {distance:.3f}]\n"
            f"{text}\n\n---\n\n"
        )

    return (
        "You are a helpful assistant. Answer the user's question based on the "
        "provided context documents. If the answer is not in the context, say so.\n\n"
        f"## Context\n\n{context_block}"
        f"## Question\n\n{question}\n\n"
        "## Answer\n\n"
    )


def ask_llm(prompt):
    """Send prompt to OpenAI (optional — prints prompt if no API key)."""
    if not OPENAI_API_KEY:
        print("\n[No OPENAI_API_KEY set — showing prompt + context only]\n")
        print(prompt)
        return None

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.1,
    )
    return response.choices[0].message.content


def run(
    question: str = "What are the main topics covered in the documents?",
    repo_path: str = "feature_repo",
    top_k: int = 5,
):
    """End-to-end RAG query via Feast."""
    from feast import FeatureStore

    print("=" * 60)
    print("STEP 3: RAG Query via Feast")
    print("=" * 60)

    store = FeatureStore(repo_path=repo_path)
    model = SentenceTransformer(EMBED_MODEL)

    print(f"Question: {question}\n")

    # Embed query
    query_embedding = embed_text(model, question)

    # Retrieve from Feast/Milvus
    context_df = search_feast(store, query_embedding, top_k=top_k)
    print(f"Retrieved {len(context_df)} chunks:\n")
    for _, row in context_df.iterrows():
        print(f"  - {row.get('file_name', '?')} (chunk {row.get('chunk_index', '?')}, "
              f"similarity: {row.get('distance', 0):.3f})")
    print()

    # Build prompt and query LLM
    prompt = build_prompt(question, context_df)
    answer = ask_llm(prompt)

    if answer:
        print(f"Answer:\n{answer}")

    print("=" * 60)
    return {"question": question, "answer": answer, "sources": context_df}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "What are the main topics covered in the documents?"
    run(question=q)

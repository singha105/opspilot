# ADR-0005: Vector store abstraction

- Status: accepted
- Date: 2026-10-05

## Context

The project compares retrieval backends (Weaviate, Chroma, optionally Pinecone) rather
than assuming one. A fair comparison needs identical inputs, and the agent code should
not care which backend answers. The three backends differ a lot: Weaviate has native
BM25 and hybrid search; Chroma and Pinecone only do dense search.

## Decision

- A `VectorStore` protocol (`src/opspilot/rag/stores/base.py`) with `upsert`,
  `dense_search`, `keyword_search`, `hybrid_search(alpha)`, `count` and `reset`. Scores
  are always "higher is better" and results carry the full chunk, so callers never
  touch backend-specific objects.
- Every store receives the **same chunks and the same vectors**: chunks are produced
  once, embedded once with `BAAI/bge-small-en-v1.5` (cached by text hash), then written
  to each backend. Differences in results come from the search implementation alone.
- Each chunker gets its own collection (`KnowledgeChunk` / `KnowledgeChunk_fixed`,
  `opspilot-kb-*` in Chroma and Pinecone), so BM25 statistics of one chunking strategy
  never leak into the other.
- **Weaviate:** self-provided vectors, BM25 over the chunk `text` property, native
  hybrid with relative-score fusion and `alpha` (1 = pure vector, 0 = pure keyword).
- **Chroma:** `PersistentClient` under `data/chroma` for dense search; keyword search
  is a `rank_bm25` index persisted next to it as JSON, tokenized like Weaviate's `word`
  tokenizer. Hybrid is weighted Reciprocal Rank Fusion (k = 60) with weights
  `alpha` / `1 - alpha`; at the default 0.5 this is standard RRF.
- **Pinecone:** only constructed when `PINECONE_API_KEY` is set (free Starter tier);
  dense search in a serverless index, keyword search and fusion as for Chroma. The
  `pinecone` client is an optional dependency group.
- Filters (`doc_types`, `categories`, `services`) are one Pydantic model translated per
  backend: Weaviate `contains_any`, Chroma boolean flag keys (`cat__X`, `svc__Y`),
  Pinecone `$in`.
- `get_store(name, chunker)` is the single factory, and one contract test suite
  (`tests/contract/`) runs against every available store, using throwaway collections.

## Alternatives considered

- **LangChain's vector store wrappers.** Convenient, but they hide how hybrid search
  and fusion work, which is exactly what this project wants to measure and explain.
- **Weaviate only.** Simpler, but a single backend gives nothing to compare, and the
  agent would be locked to a server process even for tiny tests.
- **Chroma's built-in embedding function.** It would download a different default
  model and break the "same vectors everywhere" rule; collections are created with
  `embedding_function=None`.

## Consequences

- Adding a backend means implementing six methods and passing the contract suite.
- Keyword and hybrid results for Chroma/Pinecone depend on our BM25 implementation,
  not theirs; that is stated in the results so the comparison is read correctly.
- The local BM25 index is rebuilt in memory when a store opens. At about 1,500 chunks
  that takes milliseconds; a much larger corpus would need a persistent inverted index.
- Two host-port clashes on the dev machine shaped the defaults: Weaviate HTTP is on
  8090 and gRPC on 50052, both configurable.

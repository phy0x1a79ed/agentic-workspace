import pytest


@pytest.fixture(autouse=True)
def stub_embedder():
    """Index and search with a deterministic stub, never the real model."""
    from awm.persistence import embeddings
    from awm.persistence.search_testing import StubEmbedder
    emb = StubEmbedder()
    embeddings.use_embedder(emb)
    yield emb
    embeddings.use_embedder(None)

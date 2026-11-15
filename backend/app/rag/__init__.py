"""RAG local e privado por conta.

Fluxo: arquivo (.pdf/.md/.txt) -> texto -> trechos -> embeddings via Ollama
(nomic-embed-text) -> ChromaDB. Nada disso treina ou altera o modelo: os
trechos só são recuperados e colocados no contexto da pergunta.

Isolamento: cada vetor carrega `owner_id` (e `conversation_id` quando é um
arquivo temporário). Toda busca filtra por dono — ver retriever.search_docs.
"""

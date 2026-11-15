import os
import hashlib
from pypdf import PdfReader
import chromadb
from .embedder import get_embedding
from ..config import settings


# Caminho de persistência do ChromaDB no SSD
CHROMA_PATH = settings.data_dir / "vector_db"
chroma_client = chromadb.PersistentClient(path=str(CHROMA_PATH))
collection = chroma_client.get_or_create_collection(name="distopic_docs")

def extract_text(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        reader = PdfReader(file_path)
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)
    else:
        # Suporta .md, .txt, arquivos de código, etc.
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    text = " ".join(text.split())  # Limpa quebras de linha excessivas
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

async def index_file(file_path: str) -> int:
    filename = os.path.basename(file_path)
    raw_text = extract_text(file_path)
    chunks = chunk_text(raw_text)

    if not chunks:
        return 0

    ids = []
    embeddings = []
    metadatas = []
    documents = []

    for i, chunk in enumerate(chunks):
        chunk_id = hashlib.md5(f"{filename}_{i}".encode()).hexdigest()
        emb = await get_embedding(chunk)
        
        ids.append(chunk_id)
        embeddings.append(emb)
        metadatas.append({"source": filename, "chunk_index": i})
        documents.append(chunk)

    # CORREÇÃO APONTADA PELO COPILOT: Gravando efetivamente no ChromaDB
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=documents
    )
    
    # Retorna o total de chunks indexados com sucesso
    return len(chunks)
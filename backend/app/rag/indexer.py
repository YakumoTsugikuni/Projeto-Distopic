import os
import hashlib
from pypdf import PdfReader
import chromadb
from .embedder import get_embedding

CHROMA_PATH = os.path.join("workspace", "vector_db")
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
colection = chroma_client.get_or_create_collection(name="distopic_docs")

def extract_text(flie_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        reader = PdfReader(file_path)
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)
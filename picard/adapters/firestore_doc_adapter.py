"""Firestore document adapter -- stores chunks as Firestore documents.

Each write_chunk creates a document in the picard collection with the
encoded chunk and manifest ID. Cold reads GET the document by token.

Requires: pip install google-cloud-firestore
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "firestore-doc"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class FirestoreDocAdapter(Adapter):

    def __init__(self, *, collection: str = "picard",
                 credentials_json: dict | None = None,
                 project_id: str = "") -> None:
        self._collection = collection
        self._credentials_json = credentials_json
        self._project_id = project_id
        self._db = None
        self._reg: dict[str, str] = {}

    def _get_db(self):
        if self._db is None:
            from google.cloud import firestore
            from google.oauth2 import service_account
            if self._credentials_json:
                creds = service_account.Credentials.from_service_account_info(
                    self._credentials_json,
                )
                self._db = firestore.Client(
                    project=self._project_id or self._credentials_json.get("project_id"),
                    credentials=creds,
                )
            else:
                self._db = firestore.Client(project=self._project_id or None)
        return self._db

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "google-cloud-firestore",
                  "substrate": "firestore-document"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        doc_data = {"chunk": encoded_chunk, "manifest": manifest_id, "token": token}
        db = self._get_db()
        await asyncio.to_thread(
            db.collection(self._collection).document(token).set, doc_data
        )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "collection": self._collection,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        db = self._get_db()
        snap = await asyncio.to_thread(
            db.collection(loc["collection"]).document(token).get
        )
        if not snap.exists:
            raise KeyError(f"firestore-doc chunk not found: {token}")
        chunk = snap.to_dict()["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        db = self._get_db()
        await asyncio.to_thread(
            db.collection(loc["collection"]).document(loc["token"]).delete
        )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        seen: set[str] = set()
        try:
            db = self._get_db()
            query = db.collection(self._collection).where("manifest", "==", manifest_id)
            docs = await asyncio.to_thread(lambda: list(query.stream()))
            for doc in docs:
                tok = doc.id
                if tok not in seen:
                    seen.add(tok)
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": tok,
                                              "collection": self._collection,
                                              "manifest": manifest_id}),
                    ))
        except Exception:
            pass
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id and token not in seen:
                seen.add(token)
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "collection": self._collection,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._credentials_json:
            return False
        try:
            self._get_db()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        self._db = None

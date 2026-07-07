"""Documents pipeline — limits, extraction, RBAC, audit (PR 7)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _upload(content: bytes, filename: str, mime: str, owner_type="INTAKE"):
    return {
        "files": {"file": (filename, content, mime)},
        "data": {"owner_type": owner_type, "owner_id": "REQ-TEST"},
    }


async def test_text_upload_extracts_and_audits(client):
    kw = _upload(b"Confidential agreement draft with Acme.", "draft.txt", "text/plain")
    r = await client.post("/api/v1/documents", **kw)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_extracted_text"] is True
    assert body["mime_type"] == "text/plain"

    r = await client.get("/api/v1/audit?limit=20")
    assert "document.uploaded" in {row["action"] for row in r.json()}

    r = await client.get(f"/api/v1/documents/{body['id']}")
    assert r.status_code == 200


async def test_binary_upload_stores_without_extraction(client):
    kw = _upload(b"%PDF-1.7 fake", "contract.pdf", "application/pdf", "CONTRACT")
    r = await client.post("/api/v1/documents", **kw)
    assert r.status_code == 201
    assert r.json()["has_extracted_text"] is False  # gap surfaced, not guessed


async def test_disallowed_mime_rejected(client):
    kw = _upload(b"MZ\x90\x00", "evil.exe", "application/x-msdownload")
    r = await client.post("/api/v1/documents", **kw)
    assert r.status_code == 415


async def test_oversize_rejected(client):
    kw = _upload(b"x" * (5 * 1024 * 1024 + 1), "big.txt", "text/plain")
    r = await client.post("/api/v1/documents", **kw)
    assert r.status_code == 413


async def test_requester_cannot_upload_contract_documents(client):
    kw = _upload(b"hello", "note.txt", "text/plain", "CONTRACT")
    r = await client.post(
        "/api/v1/documents",
        files=kw["files"],
        data=kw["data"],
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 403
    # …but CAN attach to their own intake filing.
    kw = _upload(b"hello", "note.txt", "text/plain", "INTAKE")
    r = await client.post(
        "/api/v1/documents",
        files=kw["files"],
        data=kw["data"],
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 201

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".csv",
    ".tsv",
    ".html",
    ".htm",
    ".xml",
    ".yml",
    ".yaml",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sql",
    ".log",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(filename: str | None) -> str:
    candidate = Path((filename or "").strip() or "upload.bin").name
    return candidate or "upload.bin"


def _guess_content_type(filename: str, content_type: str | None) -> str:
    normalized = (content_type or "").strip()
    if normalized:
        return normalized
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _guess_kind(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    normalized_type = (content_type or "").lower()
    if normalized_type.startswith("image/") or suffix in IMAGE_EXTENSIONS:
        return "image"
    if normalized_type.startswith("text/") or suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in {".pdf", ".docx"}:
        return "document"
    return "binary"


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _extract_text_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix in TEXT_EXTENSIONS:
        return _decode_text(data)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF text extraction requires pypdf to be installed") from exc
        reader = PdfReader(str(path))
        return "\n\n".join(
            (page.extract_text() or "").strip() for page in reader.pages
        ).strip()
    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError("DOCX text extraction requires python-docx to be installed") from exc
        document = Document(str(path))
        return "\n".join(
            paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()
        ).strip()
    raise RuntimeError(f"Text extraction is not supported for '{suffix or 'unknown'}' files")


@dataclass
class UploadRecord:
    upload_id: str
    filename: str
    stored_path: str
    content_type: str
    size_bytes: int
    created_at: str
    kind: str
    extracted_text_path: str | None = None
    extracted_text_status: str = "not_requested"
    extracted_text_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "UploadRecord":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            upload_id=str(data.get("upload_id") or "").strip(),
            filename=str(data.get("filename") or "").strip() or "upload.bin",
            stored_path=str(data.get("stored_path") or "").strip(),
            content_type=str(data.get("content_type") or "").strip()
            or "application/octet-stream",
            size_bytes=max(0, int(data.get("size_bytes") or 0)),
            created_at=str(data.get("created_at") or "").strip() or _utcnow_iso(),
            kind=str(data.get("kind") or "").strip() or "binary",
            extracted_text_path=str(data.get("extracted_text_path") or "").strip() or None,
            extracted_text_status=str(data.get("extracted_text_status") or "").strip()
            or "not_requested",
            extracted_text_error=str(data.get("extracted_text_error") or "").strip() or None,
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )


class UploadStore:
    def __init__(self, root_dir: str | None = None):
        self.root_dir = Path((root_dir or "").strip() or "kg_agent_uploads")
        self._lock = asyncio.Lock()

    async def save_upload(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> UploadRecord:
        upload_id = uuid.uuid4().hex
        safe_name = _safe_filename(filename)
        resolved_content_type = _guess_content_type(safe_name, content_type)
        kind = _guess_kind(safe_name, resolved_content_type)
        created_at = _utcnow_iso()

        upload_dir = self.root_dir / upload_id
        file_path = upload_dir / safe_name
        metadata_path = upload_dir / "metadata.json"

        record = UploadRecord(
            upload_id=upload_id,
            filename=safe_name,
            stored_path=str(file_path),
            content_type=resolved_content_type,
            size_bytes=len(content),
            created_at=created_at,
            kind=kind,
            metadata={
                "original_filename": filename,
            },
        )

        async with self._lock:
            await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(file_path.write_bytes, content)
            await asyncio.to_thread(
                metadata_path.write_text,
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return record

    async def get_upload(self, upload_id: str) -> UploadRecord | None:
        upload_dir = self.root_dir / (upload_id or "").strip()
        metadata_path = upload_dir / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            payload = await asyncio.to_thread(metadata_path.read_text, encoding="utf-8")
        except OSError:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return UploadRecord.from_dict(parsed)

    async def read_bytes(self, upload_id: str) -> bytes:
        record = await self.get_upload(upload_id)
        if record is None:
            raise FileNotFoundError(f"Upload '{upload_id}' not found")
        return await asyncio.to_thread(Path(record.stored_path).read_bytes)

    async def ensure_text_extract(self, upload_id: str) -> UploadRecord:
        async with self._lock:
            record = await self.get_upload(upload_id)
            if record is None:
                raise FileNotFoundError(f"Upload '{upload_id}' not found")

            if (
                record.extracted_text_status == "ready"
                and record.extracted_text_path
                and Path(record.extracted_text_path).exists()
            ):
                return record

            try:
                extracted_text = await asyncio.to_thread(
                    _extract_text_from_path,
                    Path(record.stored_path),
                )
                text_path = self.root_dir / record.upload_id / "extracted.txt"
                await asyncio.to_thread(
                    text_path.write_text,
                    extracted_text,
                    encoding="utf-8",
                )
                record.extracted_text_path = str(text_path)
                record.extracted_text_status = "ready"
                record.extracted_text_error = None
            except Exception as exc:
                record.extracted_text_path = None
                record.extracted_text_status = "unsupported"
                record.extracted_text_error = str(exc)

            await self._write_metadata(record)
            return record

    async def read_extracted_text(self, upload_id: str) -> str:
        record = await self.ensure_text_extract(upload_id)
        if record.extracted_text_status != "ready" or not record.extracted_text_path:
            raise RuntimeError(
                record.extracted_text_error
                or f"Upload '{upload_id}' does not have extracted text"
            )
        return await asyncio.to_thread(
            Path(record.extracted_text_path).read_text,
            encoding="utf-8",
        )

    async def build_attachment_context(
        self,
        upload_ids: list[str],
        *,
        max_chars_per_attachment: int = 6000,
    ) -> dict[str, Any]:
        attachments: list[dict[str, Any]] = []
        text_blocks: list[str] = []
        unsupported_multimodal = False

        for raw_upload_id in upload_ids:
            upload_id = (raw_upload_id or "").strip()
            if not upload_id:
                continue
            record = await self.get_upload(upload_id)
            if record is None:
                attachments.append(
                    {
                        "upload_id": upload_id,
                        "status": "missing",
                    }
                )
                continue

            item = {
                "upload_id": record.upload_id,
                "filename": record.filename,
                "content_type": record.content_type,
                "kind": record.kind,
                "size_bytes": record.size_bytes,
                "status": "uploaded",
            }

            if record.kind in {"text", "document"}:
                resolved = await self.ensure_text_extract(record.upload_id)
                item["text_extract_status"] = resolved.extracted_text_status
                if resolved.extracted_text_status == "ready":
                    extracted_text = await self.read_extracted_text(record.upload_id)
                    preview = extracted_text[:max_chars_per_attachment].strip()
                    if preview:
                        text_blocks.append(
                            f"[附件: {record.filename}]\n{preview}"
                        )
                        item["status"] = "ready"
                    else:
                        item["status"] = "empty_text"
                else:
                    item["status"] = resolved.extracted_text_status
                    item["error"] = resolved.extracted_text_error
            elif record.kind == "image":
                item["status"] = "unsupported_multimodal"
                unsupported_multimodal = True
            attachments.append(item)

        prompt = ""
        if text_blocks:
            prompt = "\n\n[附件上下文]\n" + "\n\n".join(text_blocks)
        return {
            "attachments": attachments,
            "prompt": prompt,
            "unsupported_multimodal": unsupported_multimodal,
        }

    async def _write_metadata(self, record: UploadRecord) -> None:
        upload_dir = self.root_dir / record.upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = upload_dir / "metadata.json"
        await asyncio.to_thread(
            metadata_path.write_text,
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

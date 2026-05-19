"""
Thin wrapper around the Google Docs + Drive REST APIs with retry on transient errors.
"""
from __future__ import annotations

from datetime import datetime

from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, HttpError) and exc.resp.status in (429, 500, 502, 503, 504)


_RETRY = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)


class GoogleDocsClient:
    def __init__(self, docs_service: object, drive_service: object) -> None:
        self.docs = docs_service
        self.drive = drive_service

    @classmethod
    def for_user(cls, creds: object) -> "GoogleDocsClient":
        """Build a client scoped to a single user's OAuth credentials."""
        from googleapiclient.discovery import build as _build
        return cls(
            _build("docs",  "v1", credentials=creds),
            _build("drive", "v3", credentials=creds),
        )

    @_RETRY
    def create_doc(self, *, title: str, parent_folder_id: str) -> str:
        body = {
            "name": title,
            "mimeType": "application/vnd.google-apps.document",
            "parents": [parent_folder_id],
        }
        return (
            self.drive.files()  # type: ignore[attr-defined]
            .create(body=body, fields="id")
            .execute()["id"]
        )

    @_RETRY
    def share(self, doc_id: str, emails: list[str], role: str = "commenter") -> None:
        for email in emails:
            try:
                (
                    self.drive.permissions()  # type: ignore[attr-defined]
                    .create(
                        fileId=doc_id,
                        body={"type": "user", "role": role, "emailAddress": email},
                        sendNotificationEmail=False,
                    )
                    .execute()
                )
            except HttpError as e:
                if e.resp.status != 409:  # 409 = already shared
                    raise

    @_RETRY
    def batch_update(self, doc_id: str, requests: list[dict]) -> dict:
        return (
            self.docs.documents()  # type: ignore[attr-defined]
            .batchUpdate(documentId=doc_id, body={"requests": requests})
            .execute()
        )

    @_RETRY
    def list_comments(self, *, doc_id: str, since: datetime) -> list[dict]:
        resp = (
            self.drive.comments()  # type: ignore[attr-defined]
            .list(
                fileId=doc_id,
                fields="comments(id,author,content,quotedFileContent,createdTime,resolved,anchor)",
                startModifiedTime=since.isoformat(),
                pageSize=100,
            )
            .execute()
        )
        return resp.get("comments", [])

    @_RETRY
    def list_named_ranges(self, *, doc_id: str) -> dict[str, dict]:
        doc = (
            self.docs.documents()  # type: ignore[attr-defined]
            .get(documentId=doc_id, fields="namedRanges")
            .execute()
        )
        return doc.get("namedRanges", {})  # name -> namedRangeGroup

    @_RETRY
    def get_doc_end_index(self, doc_id: str) -> int:
        doc = (
            self.docs.documents()  # type: ignore[attr-defined]
            .get(documentId=doc_id, fields="body")
            .execute()
        )
        return doc["body"]["content"][-1]["endIndex"] - 1

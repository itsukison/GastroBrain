"""Google Drive API client — read-only, for meeting-transcript ingestion.

Two auth modes, both via Application Default Credentials (ADC):

1. Direct user ADC (simplest, but needs the org to trust the gcloud client
   for the restricted drive.readonly scope):

       gcloud auth application-default login \\
         --scopes=openid,https://www.googleapis.com/auth/drive.readonly

2. Service-account impersonation (set GDRIVE_IMPERSONATE_SA). Use this when
   the Workspace blocks user-OAuth for Drive. The signed-in user only needs
   plain `gcloud auth application-default login` (cloud-platform scope, not
   blocked) plus Token Creator on the SA; the SA needs the Drive folder
   shared to its email and the Drive API enabled on its project. The SA
   reads the folder as itself, so no user-consent wall applies.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from gastrobrain.config import settings

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str
    size: int
    web_view_link: str


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, HttpError) and exc.resp.status in (429, 500, 502, 503, 504)


_RETRY = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)


class DriveClient:
    def __init__(self) -> None:
        import google.auth

        sa = settings.gdrive_impersonate_sa
        if sa:
            from google.auth import impersonated_credentials

            source, _ = google.auth.default()
            creds = impersonated_credentials.Credentials(
                source_credentials=source,
                target_principal=sa,
                target_scopes=[DRIVE_SCOPE],
            )
        else:
            creds, _ = google.auth.default(scopes=[DRIVE_SCOPE])
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def list_text_files(self, folder_id: str) -> Iterator[DriveFile]:
        """Yield every non-trashed text/plain file directly under `folder_id`,
        newest-first. Media (.mp4/.wav) and PDFs are excluded by the mimeType
        clause."""
        q = (
            f"'{folder_id}' in parents and trashed = false "
            f"and mimeType = 'text/plain'"
        )
        page_token: str | None = None
        while True:
            resp = self._list_page(q, page_token)
            for f in resp.get("files", []):
                yield DriveFile(
                    id=f["id"],
                    name=f.get("name", ""),
                    mime_type=f.get("mimeType", ""),
                    modified_time=f.get("modifiedTime", ""),
                    size=int(f.get("size") or 0),
                    web_view_link=f.get("webViewLink", ""),
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    @_RETRY
    def _list_page(self, q: str, page_token: str | None) -> dict:
        return (
            self._svc.files()
            .list(
                q=q,
                fields="nextPageToken, files(id,name,mimeType,modifiedTime,size,webViewLink)",
                pageSize=100,
                pageToken=page_token,
                orderBy="modifiedTime desc",
            )
            .execute()
        )

    @_RETRY
    def download_text(self, file_id: str) -> str:
        data = self._svc.files().get_media(fileId=file_id).execute()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

"""
Google Drive API wrapper for BCS trade store sync.

Low-level functions only — no business logic.
All functions raise on failure; caller decides fallback strategy.

ServiceRef wraps the Drive service and auto-reconnects on stale TLS
connections (SSL EOF errors from idle connection reuse).

Requires:
  pip install google-auth google-api-python-client
"""

import io
import json
import logging
from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.file']
# Wall-clock ceiling on any single Drive call. Comfortably above a normal
# round trip for a ~1 MB store on a home line, and far below the 5-minute cron
# interval it must not eat — a Drive hang must degrade to "local only", which
# every caller already handles, rather than to a stalled cycle.
DRIVE_TIMEOUT_SEC = 20


# ---------------------------------------------------------------------------
#  ServiceRef — auto-reconnecting Drive service wrapper
# ---------------------------------------------------------------------------

class ServiceRef:
    """Mutable wrapper around a Drive v3 service.

    Stores credentials path so the service can be recreated when the
    underlying TLS connection goes stale (SSL EOF after idle period).
    """

    def __init__(self, service, creds_path: Path):
        self.service = service
        self.creds_path = creds_path

    def reconnect(self):
        """Create a fresh Drive service (new TLS connection)."""
        creds = service_account.Credentials.from_service_account_file(
            str(self.creds_path), scopes=SCOPES
        )
        self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        logger.info("Drive service reconnected via %s", self.creds_path.name)


def _is_connection_error(exc: Exception) -> bool:
    """Check if an exception is a transient connection/SSL error."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        'eof occurred', 'ssl', 'broken pipe', 'connection reset',
        'connection aborted', 'timed out', 'read timeout',
        'remotedisconnected',
    ))


def _extract_service(svc):
    """Get raw Google API service from ServiceRef or pass through."""
    return svc.service if isinstance(svc, ServiceRef) else svc


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def get_drive_service(credentials_path: Path):
    """Build Drive v3 service from service account JSON.

    Returns a ServiceRef that supports automatic reconnection when
    the TLS connection goes stale.

    Args:
        credentials_path: Path to service account JSON key file.

    Returns:
        ServiceRef wrapping the Drive v3 resource.

    Raises:
        FileNotFoundError: If credentials file doesn't exist.
        google.auth.exceptions.DefaultCredentialsError: If JSON is invalid.
    """
    if not credentials_path.exists():
        raise FileNotFoundError(f"Credentials file not found: {credentials_path}")

    creds = service_account.Credentials.from_service_account_file(
        str(credentials_path), scopes=SCOPES
    )
    # A BOUNDED socket, because an unbounded one stops trading.
    #
    # httplib2 (underneath the Drive client) defaults to NO timeout, and no
    # timeout was set anywhere in this repo. The store uploads AFTER releasing
    # its file lock — right for the other process, but it still blocks THIS
    # cycle, and the zebra cron runs under `flock -n`, so every subsequent
    # 5-minute tick is skipped silently while one TCP connection hangs. Exit
    # monitoring stops on every open position with no alert at all. The same
    # client backs the live-money BCS store on the same Pi.
    #
    # Set on the authorized http object rather than via socket.setdefaulttimeout
    # so it cannot leak into Kite's HTTP calls or anything else in-process.
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    try:
        # google-api-python-client builds its own http; reach in and bound it.
        http = getattr(service, '_http', None)
        if http is not None and hasattr(http, 'timeout') and not http.timeout:
            http.timeout = DRIVE_TIMEOUT_SEC
            logger.debug("Drive socket timeout set to %ss", DRIVE_TIMEOUT_SEC)
    except Exception as e:                       # never block auth on this
        logger.warning("Could not set the Drive socket timeout: %s", e)
    logger.info("Drive service authenticated via %s", credentials_path.name)
    return ServiceRef(service, credentials_path)


def find_file(service, folder_id: str, file_name: str) -> Optional[str]:
    """Find file by name in a specific folder.

    Args:
        service: ServiceRef or raw Drive v3 resource.
        folder_id: Google Drive folder ID to search in.
        file_name: Exact file name to find.

    Returns:
        file_id string if found, None if not found.

    Raises:
        googleapiclient.errors.HttpError: On API failure.
    """
    def _do_find(svc):
        query = (
            f"name = '{file_name}' and "
            f"'{folder_id}' in parents and "
            f"trashed = false"
        )
        result = svc.files().list(
            q=query, spaces='drive', fields='files(id, name, modifiedTime)',
            pageSize=25,
        ).execute()

        files = result.get('files', [])
        if not files:
            logger.info("File '%s' not found in folder %s", file_name, folder_id)
            return None

        # Drive does not guarantee list ordering, so an unsorted files[0] can
        # resolve to a different duplicate on each machine — split-brain writes.
        # Sort newest-first so every caller converges on the file actually in use.
        files.sort(key=lambda f: f.get('modifiedTime', ''), reverse=True)

        if len(files) > 1:
            logger.error(
                "DUPLICATE Drive files named '%s' in folder %s — using most recently "
                "modified (%s). Retire the others; all ids: %s",
                file_name, folder_id, files[0]['id'],
                [f"{f['id']} (mod {f.get('modifiedTime')})" for f in files],
            )

        logger.debug("Found file '%s' -> %s", file_name, files[0]['id'])
        return files[0]['id']

    svc = _extract_service(service)
    try:
        return _do_find(svc)
    except Exception as e:
        if isinstance(service, ServiceRef) and _is_connection_error(e):
            logger.info("Reconnecting Drive after find_file error: %s", e)
            service.reconnect()
            return _do_find(service.service)
        raise


def download_json(service, file_id: str) -> list:
    """Download and parse a JSON file from Drive.

    Auto-retries once on transient connection errors (SSL EOF, timeout)
    by reconnecting the Drive service.

    Args:
        service: ServiceRef or raw Drive v3 resource.
        file_id: Google Drive file ID.

    Returns:
        Parsed JSON data (expected to be a list of trade dicts).

    Raises:
        googleapiclient.errors.HttpError: On API failure.
        json.JSONDecodeError: If file content is not valid JSON.
    """
    def _do_download(svc):
        request = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
        data = json.loads(buf.read().decode('utf-8'))
        if not isinstance(data, list):
            raise ValueError(f"Expected list from Drive, got {type(data).__name__}")
        return data

    svc = _extract_service(service)
    try:
        data = _do_download(svc)
    except Exception as e:
        if isinstance(service, ServiceRef) and _is_connection_error(e):
            logger.info("Reconnecting Drive after download error: %s", e)
            service.reconnect()
            data = _do_download(service.service)
        else:
            raise

    logger.info("Downloaded %d trades from Drive (file %s)", len(data), file_id)
    return data


def upload_json(service, folder_id: str, file_name: str,
                data: list, file_id: Optional[str] = None) -> str:
    """Upload JSON data to Drive. Creates new file or updates existing.

    Auto-retries once on transient connection errors (SSL EOF, timeout)
    by reconnecting the Drive service.

    Args:
        service: ServiceRef or raw Drive v3 resource.
        folder_id: Google Drive folder ID (used for new file creation).
        file_name: File name on Drive.
        data: List of trade dicts to serialize as JSON.
        file_id: Existing file ID to update. If None, creates new file.

    Returns:
        file_id of created/updated file.

    Raises:
        googleapiclient.errors.HttpError: On API failure.
    """
    def _do_upload(svc):
        content = json.dumps(data, indent=2, default=str).encode('utf-8')
        media = MediaIoBaseUpload(
            io.BytesIO(content), mimetype='application/json', resumable=False
        )

        target_id = file_id
        if not target_id:
            # Last-chance lookup before creating. A caller reaches here whenever its
            # startup find_file() came back None — which also happens on a transient
            # API miss, not just a genuinely absent file. Creating blind in that case
            # forks the store into two same-named files that then diverge silently.
            target_id = find_file(svc, folder_id, file_name)
            if target_id:
                logger.warning(
                    "upload_json called with no file_id but '%s' exists on Drive (%s) — "
                    "updating it instead of creating a duplicate", file_name, target_id
                )

        if target_id:
            result = svc.files().update(
                fileId=target_id, media_body=media
            ).execute()
            logger.info("Updated %d trades on Drive (file %s)", len(data), result['id'])
            return result['id']
        else:
            metadata = {
                'name': file_name,
                'parents': [folder_id],
                'mimeType': 'application/json',
            }
            result = svc.files().create(
                body=metadata, media_body=media, fields='id'
            ).execute()
            logger.info("Created new file on Drive: %s -> %s", file_name, result['id'])
            return result['id']

    svc = _extract_service(service)
    try:
        return _do_upload(svc)
    except Exception as e:
        if isinstance(service, ServiceRef) and _is_connection_error(e):
            logger.info("Reconnecting Drive after upload error: %s", e)
            service.reconnect()
            return _do_upload(service.service)
        raise

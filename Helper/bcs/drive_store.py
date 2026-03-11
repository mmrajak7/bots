"""
Google Drive API wrapper for BCS trade store sync.

Low-level functions only — no business logic.
All functions raise on failure; caller decides fallback strategy.

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


def get_drive_service(credentials_path: Path):
    """Build Drive v3 service from service account JSON.

    Args:
        credentials_path: Path to service account JSON key file.

    Returns:
        googleapiclient Resource for Drive v3.

    Raises:
        FileNotFoundError: If credentials file doesn't exist.
        google.auth.exceptions.DefaultCredentialsError: If JSON is invalid.
    """
    if not credentials_path.exists():
        raise FileNotFoundError(f"Credentials file not found: {credentials_path}")

    creds = service_account.Credentials.from_service_account_file(
        str(credentials_path), scopes=SCOPES
    )
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    logger.info("Drive service authenticated via %s", credentials_path.name)
    return service


def find_file(service, folder_id: str, file_name: str) -> Optional[str]:
    """Find file by name in a specific folder.

    Args:
        service: Drive v3 service resource.
        folder_id: Google Drive folder ID to search in.
        file_name: Exact file name to find.

    Returns:
        file_id string if found, None if not found.

    Raises:
        googleapiclient.errors.HttpError: On API failure.
    """
    query = (
        f"name = '{file_name}' and "
        f"'{folder_id}' in parents and "
        f"trashed = false"
    )
    result = service.files().list(
        q=query, spaces='drive', fields='files(id, name)',
        pageSize=5,
    ).execute()

    files = result.get('files', [])
    if not files:
        logger.info("File '%s' not found in folder %s", file_name, folder_id)
        return None

    if len(files) > 1:
        logger.warning("Multiple files named '%s' in folder — using first", file_name)

    logger.debug("Found file '%s' -> %s", file_name, files[0]['id'])
    return files[0]['id']


def download_json(service, file_id: str) -> list:
    """Download and parse a JSON file from Drive.

    Args:
        service: Drive v3 service resource.
        file_id: Google Drive file ID.

    Returns:
        Parsed JSON data (expected to be a list of trade dicts).

    Raises:
        googleapiclient.errors.HttpError: On API failure.
        json.JSONDecodeError: If file content is not valid JSON.
    """
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buf.seek(0)
    data = json.loads(buf.read().decode('utf-8'))
    if not isinstance(data, list):
        raise ValueError(f"Expected list from Drive, got {type(data).__name__}")
    logger.info("Downloaded %d trades from Drive (file %s)", len(data), file_id)
    return data


def upload_json(service, folder_id: str, file_name: str,
                data: list, file_id: Optional[str] = None) -> str:
    """Upload JSON data to Drive. Creates new file or updates existing.

    Args:
        service: Drive v3 service resource.
        folder_id: Google Drive folder ID (used for new file creation).
        file_name: File name on Drive.
        data: List of trade dicts to serialize as JSON.
        file_id: Existing file ID to update. If None, creates new file.

    Returns:
        file_id of created/updated file.

    Raises:
        googleapiclient.errors.HttpError: On API failure.
    """
    content = json.dumps(data, indent=2, default=str).encode('utf-8')
    media = MediaIoBaseUpload(
        io.BytesIO(content), mimetype='application/json', resumable=False
    )

    if file_id:
        result = service.files().update(
            fileId=file_id, media_body=media
        ).execute()
        logger.info("Updated %d trades on Drive (file %s)", len(data), result['id'])
        return result['id']
    else:
        metadata = {
            'name': file_name,
            'parents': [folder_id],
            'mimeType': 'application/json',
        }
        result = service.files().create(
            body=metadata, media_body=media, fields='id'
        ).execute()
        logger.info("Created new file on Drive: %s -> %s", file_name, result['id'])
        return result['id']

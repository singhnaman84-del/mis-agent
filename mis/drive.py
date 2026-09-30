"""Google Drive sync (service account, read-only).

Setup (once): create a service account in Google Cloud, enable the Drive API, download its JSON key, and share the
Drive folder that holds the MIS files with the service account's e-mail (Viewer). Put the JSON in the app secrets as
`gcp_service_account` and the folder id as `DRIVE_FOLDER_ID` (optional - without it every file shared with the
service account is searched).

Only the newest file of each MIS type is downloaded; files already cached with the same modifiedTime are reused.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re

from .loaders import FILE_TYPES
from .util import date_from_name

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET = "application/vnd.google-apps.spreadsheet"


def _service(sa_info: dict):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_info = dict(sa_info)
    pk = str(sa_info.get("private_key", "")).strip().strip('"').replace("\\n", "\n")
    if "BEGIN PRIVATE KEY" not in pk or "END PRIVATE KEY" not in pk:
        raise ValueError("private_key in secrets is incomplete - copy the whole value from the JSON file, from "
                         "-----BEGIN PRIVATE KEY----- to -----END PRIVATE KEY----- (no '...' placeholders)")
    sa_info["private_key"] = pk
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_mis_files(sa_info: dict, folder_id: str | None = None) -> list[dict]:
    svc = _service(sa_info)
    # NB: Drive's "name contains" matches whole words, so ".xls" never matches - list every non-folder file and
    # let the MIS file-name patterns (loaders.FILE_TYPES) pick the ones we need.
    q = "trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    if folder_id:
        q += f" and '{folder_id}' in parents"
    files, token = [], None
    while True:
        r = svc.files().list(q=q, pageSize=200, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
                             fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)").execute()
        files += r.get("files", [])
        token = r.get("nextPageToken")
        if not token: break
    out = []
    for f in files:
        for k, (rx, label, book) in FILE_TYPES.items():
            if re.search(rx, f["name"], re.I):
                out.append({**f, "type": k, "label": label, "asof": date_from_name(f["name"])})
                break
    return out


def newest_per_type(files: list[dict]) -> dict[str, dict]:
    best = {}
    for f in files:
        key = (f["asof"] or dt.date.min, "north zone" not in f["name"].lower(), f["modifiedTime"])
        if f["type"] not in best or key > best[f["type"]][0]:
            best[f["type"]] = (key, f)
    return {k: v[1] for k, v in best.items()}


def sync(sa_info: dict, dest: str, folder_id: str | None = None, progress=None) -> dict:
    """Download the newest file of each type into `dest`. Returns {type: local path}."""
    from googleapiclient.http import MediaIoBaseDownload
    os.makedirs(dest, exist_ok=True)
    svc = _service(sa_info)
    pick = newest_per_type(list_mis_files(sa_info, folder_id))
    manifest_p = os.path.join(dest, "_manifest.json")
    manifest = json.load(open(manifest_p)) if os.path.exists(manifest_p) else {}
    keep = set()
    for i, (k, f) in enumerate(pick.items()):
        name = f["name"] if f["mimeType"] != GSHEET else f["name"] + ".xlsx"
        path = os.path.join(dest, name)
        keep.add(name)
        if progress: progress(i / max(1, len(pick)), f["name"])
        if manifest.get(name) == f["modifiedTime"] and os.path.exists(path):
            continue
        req = (svc.files().export_media(fileId=f["id"], mimeType=XLSX) if f["mimeType"] == GSHEET
               else svc.files().get_media(fileId=f["id"], supportsAllDrives=True))
        buf = io.FileIO(path, "wb")
        dl = MediaIoBaseDownload(buf, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
        buf.close()
        manifest[name] = f["modifiedTime"]
    # drop superseded files so the loader never picks an old drop
    for fn in os.listdir(dest):
        if fn != "_manifest.json" and fn not in keep:
            try: os.remove(os.path.join(dest, fn))
            except OSError: pass
    json.dump({k: v for k, v in manifest.items() if k in keep}, open(manifest_p, "w"))
    return {k: os.path.join(dest, f["name"] if f["mimeType"] != GSHEET else f["name"] + ".xlsx") for k, f in pick.items()}

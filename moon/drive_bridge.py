from __future__ import annotations

import hashlib
import io
import json
import math
import mimetypes
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from moon.agent_bridge import AgentBridgeService
from moon.handoff import AgentHandoffService
from moon.runner.pipeline import PipelineRunner

BRIDGE_VERSION = "1.0"
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SAFE_EVIDENCE_SUFFIXES = {".json", ".jpg", ".jpeg", ".png", ".webp", ".txt"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
REMOTE_ROOT_NAME = "MON_EDIT"


class BridgeError(RuntimeError):
    pass


class BridgeTransportError(BridgeError):
    pass


class BridgeResponseError(BridgeError):
    pass


class DuplicateResponseError(BridgeResponseError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BridgeResponseError(f"response {field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeResponseError(f"response {field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise BridgeResponseError(f"response {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _expand_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DriveBridgeConfig:
    project_id: str
    transport: str = "google_drive_api"
    poll_interval_seconds: float = 10.0
    stale_after_seconds: int = 86400
    drive_root_folder_id: str | None = None
    credentials_path: Path | None = None
    token_path: Path | None = None
    sync_root: Path | None = None
    max_evidence_files: int = 100
    max_evidence_bytes: int = 25 * 1024 * 1024

    @classmethod
    def load(cls, project_root: Path) -> DriveBridgeConfig:
        path = project_root / ".moon" / "bridge.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Moon Drive bridge config not found: {path}. See docs/MOON_LOCAL_RUNTIME.md."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(".moon/bridge.json must contain one JSON object")
        drive = payload.get("drive") or {}
        if not isinstance(drive, dict):
            raise TypeError("bridge config drive must be an object")
        config = cls(
            project_id=str(payload.get("project_id") or project_root.name),
            transport=str(payload.get("transport") or "google_drive_api"),
            poll_interval_seconds=float(payload.get("poll_interval_seconds", 10.0)),
            stale_after_seconds=int(payload.get("stale_after_seconds", 86400)),
            drive_root_folder_id=os.environ.get("MOON_DRIVE_ROOT_FOLDER_ID")
            or drive.get("root_folder_id"),
            credentials_path=_expand_path(
                os.environ.get("MOON_DRIVE_CREDENTIALS") or drive.get("credentials_path")
            ),
            token_path=_expand_path(os.environ.get("MOON_DRIVE_TOKEN") or drive.get("token_path")),
            sync_root=_expand_path(drive.get("sync_root")),
            max_evidence_files=int(payload.get("max_evidence_files", 100)),
            max_evidence_bytes=int(payload.get("max_evidence_bytes", 25 * 1024 * 1024)),
        )
        config.validate(project_root)
        return config

    def validate(self, project_root: Path) -> None:
        if not SAFE_ID.fullmatch(self.project_id):
            raise ValueError("bridge project_id must use only letters, numbers, '.', '_' or '-'")
        if self.transport not in {"google_drive_api", "local_sync"}:
            raise ValueError("bridge transport must be google_drive_api or local_sync")
        if not math.isfinite(self.poll_interval_seconds) or self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than zero")
        if self.max_evidence_files < 0 or self.max_evidence_bytes < 0:
            raise ValueError("evidence limits must not be negative")
        if self.transport == "google_drive_api":
            if not self.drive_root_folder_id:
                raise ValueError("drive.root_folder_id is required for Google Drive API transport")
            if not SAFE_DRIVE_ID.fullmatch(self.drive_root_folder_id):
                raise ValueError("drive.root_folder_id is not a valid Google Drive ID")
            if not self.credentials_path:
                raise ValueError("drive.credentials_path or MOON_DRIVE_CREDENTIALS is required")
            for label, path in (("credentials_path", self.credentials_path), ("token_path", self.token_path)):
                if path and _is_within(path, project_root):
                    raise ValueError(f"drive.{label} must be outside the Moon project")
                if path and _inside_git_worktree(path):
                    raise ValueError(f"drive.{label} must be outside a Git worktree")
        else:
            if not self.sync_root:
                raise ValueError("drive.sync_root is required for local_sync transport")
            if _is_within(self.sync_root, project_root) or _is_within(project_root, self.sync_root):
                raise ValueError("drive.sync_root and the Moon project must not contain each other")

    @property
    def remote_path(self) -> str:
        return f"{REMOTE_ROOT_NAME}/jobs/{self.project_id}/AGENT"


class BridgeTransport(Protocol):
    def publish(self, request_path: Path, evidence: list[tuple[Path, str]]) -> dict[str, Any]: ...

    def download_response(self) -> bytes | None: ...

    def upload_request(self, request_path: Path) -> None: ...

    def upload_response(self, response_path: Path) -> None: ...

    def status(self) -> dict[str, Any]: ...


class LocalSyncTransport:
    """Optional Google Drive for Desktop transport; sync_root is the My Drive directory."""

    def __init__(self, config: DriveBridgeConfig) -> None:
        assert config.sync_root is not None
        self.config = config
        self.remote = config.sync_root / REMOTE_ROOT_NAME / "jobs" / config.project_id / "AGENT"

    def publish(self, request_path: Path, evidence: list[tuple[Path, str]]) -> dict[str, Any]:
        try:
            self.remote.mkdir(parents=True, exist_ok=True)
            self.upload_request(request_path)
            for source, relative in evidence:
                target = self._target(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        except OSError as exc:
            raise BridgeTransportError(f"could not publish to Drive sync folder: {exc}") from exc
        return {"transport": "local_sync", "remote_path": str(self.remote), "files": 1 + len(evidence)}

    def download_response(self) -> bytes | None:
        path = self.remote / "response.json"
        try:
            if not path.exists():
                return None
            if path.stat().st_size > MAX_RESPONSE_BYTES:
                raise BridgeResponseError("Drive response exceeds the 5 MiB safety limit")
            return path.read_bytes()
        except OSError as exc:
            raise BridgeTransportError(f"could not read Drive sync response: {exc}") from exc

    def upload_request(self, request_path: Path) -> None:
        try:
            self.remote.mkdir(parents=True, exist_ok=True)
            shutil.copy2(request_path, self.remote / "request.json")
        except OSError as exc:
            raise BridgeTransportError(f"could not update Drive sync request: {exc}") from exc

    def upload_response(self, response_path: Path) -> None:
        try:
            self.remote.mkdir(parents=True, exist_ok=True)
            shutil.copy2(response_path, self.remote / "response.json")
        except OSError as exc:
            raise BridgeTransportError(f"could not update Drive sync response: {exc}") from exc

    def status(self) -> dict[str, Any]:
        response = self.remote / "response.json"
        return {
            "transport": "local_sync",
            "remote_path": str(self.remote),
            "response_present": response.is_file(),
        }

    def _target(self, relative: str) -> Path:
        target = (self.remote / Path(relative)).resolve()
        if not _is_within(target, self.remote):
            raise BridgeTransportError(f"unsafe packet path: {relative!r}")
        return target


class GoogleDriveTransport:
    """Small Drive v3 adapter that can only read/write the configured AGENT folder."""

    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, config: DriveBridgeConfig, *, service: Any | None = None) -> None:
        self.config = config
        self._service = service
        self._agent_folder_id: str | None = None

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def publish(self, request_path: Path, evidence: list[tuple[Path, str]]) -> dict[str, Any]:
        folder = self._agent_folder()
        self._upload_file(request_path, "request.json", folder)
        for source, relative in evidence:
            parts = Path(relative).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise BridgeTransportError(f"unsafe packet path: {relative!r}")
            parent = folder
            for directory in parts[:-1]:
                parent = self._find_or_create_folder(parent, directory)
            self._upload_file(source, parts[-1], parent)
        return {"transport": "google_drive_api", "remote_path": self.config.remote_path, "files": 1 + len(evidence)}

    def download_response(self) -> bytes | None:
        metadata = self._find_one(self._agent_folder(), "response.json")
        if metadata is None:
            return None
        size = int(metadata.get("size") or 0)
        if size > MAX_RESPONSE_BYTES:
            raise BridgeResponseError("Drive response exceeds the 5 MiB safety limit")
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:  # pragma: no cover - exercised by configured installations
            raise BridgeTransportError(
                "Google Drive transport requires google-api-python-client and google-auth-oauthlib"
            ) from exc
        handle = io.BytesIO()
        try:
            request = self.service.files().get_media(fileId=metadata["id"], supportsAllDrives=True)
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
                if handle.tell() > MAX_RESPONSE_BYTES:
                    raise BridgeResponseError("Drive response exceeds the 5 MiB safety limit")
        except BridgeResponseError:
            raise
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not download Drive response: {exc}") from exc
        return handle.getvalue()

    def upload_request(self, request_path: Path) -> None:
        self._upload_file(request_path, "request.json", self._agent_folder())

    def upload_response(self, response_path: Path) -> None:
        self._upload_file(response_path, "response.json", self._agent_folder())

    def status(self) -> dict[str, Any]:
        response = self._find_one(self._agent_folder(), "response.json")
        return {
            "transport": "google_drive_api",
            "remote_path": self.config.remote_path,
            "folder_id": self._agent_folder(),
            "response_present": response is not None,
            "response_modified_at": response.get("modifiedTime") if response else None,
        }

    def _agent_folder(self) -> str:
        if self._agent_folder_id is None:
            assert self.config.drive_root_folder_id is not None
            self._validate_root_folder(self.config.drive_root_folder_id)
            jobs = self._find_or_create_folder(self.config.drive_root_folder_id, "jobs")
            project = self._find_or_create_folder(jobs, self.config.project_id)
            self._agent_folder_id = self._find_or_create_folder(project, "AGENT")
        return self._agent_folder_id

    def _validate_root_folder(self, folder_id: str) -> None:
        try:
            metadata = self.service.files().get(
                fileId=folder_id,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not access configured MON_EDIT folder: {exc}") from exc
        if metadata.get("mimeType") != self.FOLDER_MIME or metadata.get("name") != REMOTE_ROOT_NAME:
            raise BridgeTransportError("drive.root_folder_id must identify a folder named MON_EDIT")

    def _find_or_create_folder(self, parent_id: str, name: str) -> str:
        existing = self._find_one(parent_id, name, mime_type=self.FOLDER_MIME)
        if existing:
            return str(existing["id"])
        try:
            created = self.service.files().create(
                body={"name": name, "mimeType": self.FOLDER_MIME, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not create Drive folder {name!r}: {exc}") from exc
        return str(created["id"])

    def _find_one(self, parent_id: str, name: str, *, mime_type: str | None = None) -> dict[str, Any] | None:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"'{parent_id}' in parents and name = '{escaped}' and trashed = false"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        try:
            result = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id,name,mimeType,modifiedTime,size,md5Checksum)",
                pageSize=10,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not list Drive folder: {exc}") from exc
        files = result.get("files") or []
        if len(files) > 1:
            raise BridgeTransportError(f"duplicate Drive entries named {name!r} in the AGENT path")
        return files[0] if files else None

    def _upload_file(self, source: Path, name: str, parent_id: str) -> None:
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover - exercised by configured installations
            raise BridgeTransportError(
                "Google Drive transport requires google-api-python-client and google-auth-oauthlib"
            ) from exc
        existing = self._find_one(parent_id, name)
        media = MediaFileUpload(
            str(source),
            mimetype=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            resumable=False,
        )
        try:
            if existing:
                self.service.files().update(
                    fileId=existing["id"], media_body=media, supportsAllDrives=True
                ).execute()
            else:
                self.service.files().create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not upload {name!r} to Drive: {exc}") from exc

    def _build_service(self) -> Any:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - exercised by configured installations
            raise BridgeTransportError(
                "Google Drive transport requires google-api-python-client and google-auth-oauthlib"
            ) from exc
        assert self.config.credentials_path is not None
        if not self.config.credentials_path.is_file():
            raise BridgeTransportError(f"Google credentials file not found: {self.config.credentials_path}")
        try:
            raw = json.loads(self.config.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeTransportError(f"could not read Google credentials: {exc}") from exc
        credential_type = raw.get("type") if isinstance(raw, dict) else None
        if credential_type == "service_account":
            credentials = service_account.Credentials.from_service_account_file(
                str(self.config.credentials_path), scopes=DRIVE_SCOPES
            )
        elif credential_type == "authorized_user":
            credentials = Credentials.from_authorized_user_file(
                str(self.config.credentials_path), DRIVE_SCOPES
            )
        elif isinstance(raw, dict) and ("installed" in raw or "web" in raw):
            if not self.config.token_path:
                raise BridgeTransportError("drive.token_path is required for OAuth desktop credentials")
            if self.config.token_path and self.config.token_path.is_file():
                credentials = Credentials.from_authorized_user_file(
                    str(self.config.token_path), DRIVE_SCOPES
                )
            else:
                credentials = None
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            if not credentials or not credentials.valid:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.config.credentials_path), DRIVE_SCOPES
                )
                credentials = flow.run_local_server(port=0)
            _atomic_text(self.config.token_path, credentials.to_json())
        else:
            raise BridgeTransportError(
                "Google credentials must be an OAuth desktop client, authorized-user token, or service account JSON"
            )
        try:
            return build("drive", "v3", credentials=credentials, cache_discovery=False)
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not initialize Google Drive API: {exc}") from exc


class MoonDriveBridge:
    def __init__(
        self,
        runner: PipelineRunner,
        config: DriveBridgeConfig,
        *,
        transport: BridgeTransport | None = None,
        resume: Callable[[], dict[str, Any]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        config.validate(runner.project.root)
        self.runner = runner
        self.config = config
        self.agent_dir = runner.project.root / "AGENT"
        self.request_path = self.agent_dir / "request.json"
        self.response_path = self.agent_dir / "response.json"
        self.state_path = runner.project.moon_dir / "bridge-state.json"
        self.transport = transport or self._transport_for(config)
        self._resume = resume or (lambda: AgentBridgeService(self.runner).next())
        self._sleep = sleeper

    def publish(self, stage: str) -> dict[str, Any]:
        state = self._read_state()
        active = state.get("active_request") or {}
        if active.get("stage") == stage and active.get("status") == "CONSUMED":
            request_id = str(active.get("request_id") or "")
            resume = self._resume_pending(state, request_id)
            return {
                "status": "CONSUMED",
                "idempotent": True,
                "request": self._read_json(self.request_path),
                "resume": resume,
            }
        if (
            active.get("stage") == stage
            and active.get("status") == "WAITING_AGENT"
            and self.runner.state.next_stage() == stage
            and self.request_path.is_file()
        ):
            request = self._read_json(self.request_path)
            evidence = self._evidence_paths(request)
            remote = self.transport.publish(self.request_path, evidence)
            return {"status": "WAITING_AGENT", "idempotent": True, "request": request, "remote": remote}

        handoff = AgentHandoffService(self.runner).package(stage)
        request_id = uuid.uuid4().hex
        created = _utc_now()
        expires = created + timedelta(seconds=self.config.stale_after_seconds)
        evidence = self._stage_evidence(request_id, handoff)
        request = {
            "version": BRIDGE_VERSION,
            "job_id": self.config.project_id,
            "request_id": request_id,
            "stage": stage,
            "status": "WAITING_AGENT",
            "created_at": _iso(created),
            "updated_at": _iso(created),
            "expires_at": _iso(expires),
            "task": self._compact_task(handoff["task"]),
            "evidence": [descriptor for _, _, descriptor in evidence],
            "expected_response_schema": self._response_schema(handoff["output_contract"]),
        }
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self._archive_previous_response(state)
        _atomic_json(self.request_path, request)
        state["active_request"] = {
            "job_id": self.config.project_id,
            "request_id": request_id,
            "stage": stage,
            "status": "WAITING_AGENT",
            "created_at": request["created_at"],
            "expires_at": request["expires_at"],
        }
        self._write_state(state)
        remote = self.transport.publish(
            self.request_path, [(source, relative) for source, relative, _ in evidence]
        )
        return {"status": "WAITING_AGENT", "idempotent": False, "request": request, "remote": remote}

    def poll_once(self) -> dict[str, Any] | None:
        raw = self.transport.download_response()
        if raw is None:
            return None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BridgeResponseError("Drive response exceeds the 5 MiB safety limit")
        response = self._parse_response(raw)
        state = self._read_state()
        active = state.get("active_request") or {}
        consumed = state.get("consumed") or {}
        request_id = response.get("request_id")
        if request_id in consumed:
            was_pending = bool(consumed[request_id].get("resume_pending"))
            resume = self._resume_pending(state, request_id)
            if was_pending:
                return {
                    "status": "CONSUMED" if resume and resume.get("status") != "resume_pending" else "CONSUMED_RESUME_PENDING",
                    "request_id": request_id,
                    "stage": consumed[request_id].get("stage"),
                    "submission": {"accepted": False, "duplicate": True},
                    "resume": resume,
                    "warnings": [],
                }
            raise DuplicateResponseError(
                f"response for request_id {request_id!r} was already consumed"
            )
        self._validate_response(response, active)
        response_hash = _sha256_bytes(raw)
        submission = AgentHandoffService(self.runner).submit(response["stage"], response["payload"])
        consumed_at = _iso(_utc_now())
        consumed_response = dict(response)
        consumed_response["status"] = "CONSUMED"
        consumed_response["consumed_at"] = consumed_at
        consumed_response["updated_at"] = consumed_at
        _atomic_json(self.response_path, consumed_response)
        request = self._read_json(self.request_path)
        request["status"] = "CONSUMED"
        request["updated_at"] = consumed_at
        _atomic_json(self.request_path, request)
        consumed[request_id] = {
            "response_sha256": response_hash,
            "stage": response["stage"],
            "consumed_at": consumed_at,
            "resume_pending": True,
        }
        state["consumed"] = dict(list(consumed.items())[-100:])
        state["active_request"] = {
            **active,
            "status": "CONSUMED",
            "consumed_at": consumed_at,
        }
        self._write_state(state)
        sync_warnings: list[str] = []
        try:
            self.transport.upload_response(self.response_path)
            self.transport.upload_request(self.request_path)
        except BridgeTransportError as exc:
            sync_warnings.append(str(exc))
        resume = self._resume_pending(state, request_id)
        return {
            "status": "CONSUMED" if resume and resume.get("status") != "resume_pending" else "CONSUMED_RESUME_PENDING",
            "request_id": request_id,
            "stage": response["stage"],
            "submission": submission,
            "resume": resume,
            "warnings": sync_warnings,
        }

    def watch(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        reconnects = 0
        last_error: str | None = None
        while True:
            try:
                result = self.poll_once()
                if result is not None:
                    result["reconnects"] = reconnects
                    return result
            except BridgeTransportError as exc:
                reconnects += 1
                last_error = str(exc)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for Drive response"
                    + (f"; last transport error: {last_error}" if last_error else "")
                )
            self._sleep(self.config.poll_interval_seconds)

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        return {
            "job_id": self.config.project_id,
            "local_agent_dir": str(self.agent_dir),
            "remote_path": self.config.remote_path,
            "active_request": state.get("active_request"),
            "consumed_count": len(state.get("consumed") or {}),
            "remote": self.transport.status(),
        }

    def _resume_pending(self, state: dict[str, Any], request_id: str) -> dict[str, Any] | None:
        entry = (state.get("consumed") or {}).get(request_id)
        if not entry or not entry.get("resume_pending"):
            return None
        stage = str(entry.get("stage") or "")
        if self.runner.state.next_stage() != stage:
            entry["resume_pending"] = False
            entry["resume_result"] = {"status": "already_advanced", "pipeline": self.runner.status()}
            self._write_state(state)
            return entry["resume_result"]
        try:
            result = self._resume()
        except Exception as exc:  # noqa: BLE001 -- persist a retry marker for any runtime failure
            entry["resume_error"] = str(exc)
            self._write_state(state)
            return {"status": "resume_pending", "error": str(exc)}
        entry["resume_pending"] = False
        entry["resume_result"] = result
        entry.pop("resume_error", None)
        self._write_state(state)
        return result

    def _stage_evidence(
        self, request_id: str, handoff: dict[str, Any]
    ) -> list[tuple[Path, str, dict[str, Any]]]:
        candidates: list[tuple[str, Path, dict[str, Any]]] = []
        for name, value in handoff.get("inputs", {}).items():
            if name != "evidence" and isinstance(value, dict) and value.get("path"):
                candidates.append(
                    (
                        f"inputs/{name}{Path(value['path']).suffix}",
                        Path(value["path"]),
                        {"role": "input_artifact", "artifact": name},
                    )
                )
        evidence_input = handoff.get("inputs", {}).get("evidence") or {}
        sampled_metadata: dict[Path, dict[str, Any]] = {}
        if isinstance(evidence_input, dict):
            sampled = evidence_input.get("sampled_frames") or {}
            for group in sampled.get("groups") or []:
                source = group.get("source") or {}
                for frame in group.get("frames") or []:
                    absolute = frame.get("absolute_path")
                    if absolute:
                        sampled_metadata[Path(str(absolute)).resolve()] = {
                            "role": "sampled_frame",
                            "clip_id": source.get("clip_id"),
                            "group_id": group.get("group_id"),
                            "timestamp_seconds": frame.get("timestamp_seconds"),
                        }
            for item in evidence_input.get("files") or []:
                source = Path(str(item))
                try:
                    relative = source.resolve().relative_to(self.runner.project.root.resolve())
                except (OSError, ValueError):
                    continue
                metadata = sampled_metadata.get(source.resolve(), {"role": "stage_evidence"})
                candidates.append((str(Path("project") / relative), source, metadata))

        result: list[tuple[Path, str, dict[str, Any]]] = []
        seen: set[Path] = set()
        total_bytes = 0
        for relative_hint, source, metadata in candidates:
            try:
                resolved = source.expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved in seen or not resolved.is_file() or not _is_within(resolved, self.runner.project.root):
                continue
            if resolved.suffix.lower() not in SAFE_EVIDENCE_SUFFIXES:
                continue
            size = resolved.stat().st_size
            if len(result) >= self.config.max_evidence_files or total_bytes + size > self.config.max_evidence_bytes:
                continue
            relative = Path("evidence") / request_id / _safe_relative(relative_hint)
            target = self.agent_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, target)
            descriptor = {
                "path": relative.as_posix(),
                "sha256": _sha256(target),
                "bytes": size,
                "media_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                **metadata,
            }
            result.append((target, relative.as_posix(), descriptor))
            seen.add(resolved)
            total_bytes += size
        return result

    def _validate_response(self, response: dict[str, Any], active: dict[str, Any]) -> None:
        allowed = {
            "version",
            "job_id",
            "request_id",
            "stage",
            "status",
            "payload",
            "created_at",
            "updated_at",
        }
        unknown = sorted(set(response) - allowed)
        if unknown:
            raise BridgeResponseError(f"response contains unsupported fields: {', '.join(unknown)}")
        required = {"version", "job_id", "request_id", "stage", "status", "payload", "created_at"}
        missing = sorted(required - set(response))
        if missing:
            raise BridgeResponseError(f"response is missing required fields: {', '.join(missing)}")
        if response["version"] != BRIDGE_VERSION:
            raise BridgeResponseError(f"unsupported response version: {response['version']!r}")
        for field in ("job_id", "request_id", "stage"):
            if response[field] != active.get(field):
                raise BridgeResponseError(
                    f"response {field} {response[field]!r} does not match active request {active.get(field)!r}"
                )
        if response["status"] != "COMPLETED":
            raise BridgeResponseError("response status must be COMPLETED")
        if not isinstance(response["payload"], dict):
            raise BridgeResponseError("response payload must be a JSON object")
        created = _parse_timestamp(response["created_at"], "created_at")
        request_created = _parse_timestamp(active.get("created_at"), "request created_at")
        expires = _parse_timestamp(active.get("expires_at"), "request expires_at")
        now = _utc_now()
        if created < request_created:
            raise BridgeResponseError("response is stale: created before the active request")
        if created > now + timedelta(minutes=5):
            raise BridgeResponseError("response created_at is unreasonably far in the future")
        if now > expires:
            raise BridgeResponseError("response is stale: active request has expired")

    @staticmethod
    def _response_schema(output_contract: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "version",
                "job_id",
                "request_id",
                "stage",
                "status",
                "created_at",
                "payload",
            ],
            "properties": {
                "version": {"const": BRIDGE_VERSION},
                "job_id": {"type": "string"},
                "request_id": {"type": "string"},
                "stage": {"type": "string"},
                "status": {"const": "COMPLETED"},
                "created_at": {"type": "string", "format": "date-time"},
                "updated_at": {"type": "string", "format": "date-time"},
                "payload": output_contract,
            },
        }

    @staticmethod
    def _compact_task(task: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "stage",
            "revision",
            "decision_owner",
            "required_output_artifact",
            "required_output_artifacts",
            "quality_gate",
            "render_integrity",
            "instruction",
        }
        return {key: value for key, value in task.items() if key in allowed}

    @staticmethod
    def _parse_response(raw: bytes) -> dict[str, Any]:
        def reject_constant(value: str) -> None:
            raise BridgeResponseError(f"response JSON contains non-finite number {value}")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise BridgeResponseError(f"response JSON contains duplicate key {key!r}")
                result[key] = value
            return result

        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=reject_duplicates,
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise BridgeResponseError(f"malformed Drive response JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BridgeResponseError("Drive response must be one JSON object")
        return payload

    def _evidence_paths(self, request: dict[str, Any]) -> list[tuple[Path, str]]:
        result: list[tuple[Path, str]] = []
        for descriptor in request.get("evidence") or []:
            relative = str(descriptor.get("path") or "")
            source = (self.agent_dir / relative).resolve()
            if not _is_within(source, self.agent_dir) or not source.is_file():
                raise BridgeError(f"published evidence is missing or unsafe: {relative!r}")
            if _sha256(source) != descriptor.get("sha256"):
                raise BridgeError(f"published evidence checksum changed: {relative!r}")
            result.append((source, relative))
        return result

    def _archive_previous_response(self, state: dict[str, Any]) -> None:
        if not self.response_path.exists():
            return
        old_request_id = str((state.get("active_request") or {}).get("request_id") or "unknown")
        history = self.runner.project.moon_dir / "bridge-history"
        history.mkdir(parents=True, exist_ok=True)
        target = history / f"{old_request_id}-response.json"
        if not target.exists():
            self.response_path.replace(target)
        else:
            self.response_path.unlink()

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": BRIDGE_VERSION, "consumed": {}}
        payload = self._read_json(self.state_path)
        if not isinstance(payload.get("consumed", {}), dict):
            raise BridgeError("invalid local bridge state: consumed must be an object")
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        state["version"] = BRIDGE_VERSION
        state["updated_at"] = _iso(_utc_now())
        _atomic_json(self.state_path, state)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise BridgeError(f"expected JSON object in {path}")
        return payload

    @staticmethod
    def _transport_for(config: DriveBridgeConfig) -> BridgeTransport:
        if config.transport == "local_sync":
            return LocalSyncTransport(config)
        return GoogleDriveTransport(config)


def _safe_relative(value: str) -> Path:
    parts = []
    for part in Path(value).parts:
        if part in {"", ".", "..", "\\", "/"}:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._") or "item"
        parts.append(cleaned)
    return Path(*parts) if parts else Path("evidence")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _inside_git_worktree(path: Path) -> bool:
    for parent in (path.parent, *path.parent.parents):
        if (parent / ".git").exists():
            return True
    return False


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass

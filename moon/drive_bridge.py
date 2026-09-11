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
from moon.agent_state import AgentStateStore, transition, utc_now
from moon.footage_refinement import (
    FOOTAGE_REFINEMENT_REQUEST_SCHEMA,
    FootageRefinementService,
)
from moon.footage_batches import FootageBatchPolicy, FootageSemanticProgress
from moon.gemini_handoff import GeminiHandoffPacketBuilder
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
    max_footage_batch_clips: int = 3
    max_footage_batch_ranges: int = 3
    max_footage_batch_frames: int = 60
    max_footage_batch_bytes: int = 24 * 1024 * 1024

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
            max_footage_batch_clips=int(payload.get("max_footage_batch_clips", 3)),
            max_footage_batch_ranges=int(payload.get("max_footage_batch_ranges", 3)),
            max_footage_batch_frames=int(payload.get("max_footage_batch_frames", 60)),
            max_footage_batch_bytes=int(
                payload.get("max_footage_batch_bytes", 24 * 1024 * 1024)
            ),
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
        if min(
            self.max_footage_batch_clips,
            self.max_footage_batch_ranges,
            self.max_footage_batch_frames,
            self.max_footage_batch_bytes,
        ) <= 0:
            raise ValueError("footage batch limits must be positive")
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

    def archive_response(self, request_id: str, revision: int) -> None: ...

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
            incoming = json.loads(request_path.read_text(encoding="utf-8"))
            previous_path = self._target("request.json")
            previous = {}
            if previous_path.is_file():
                try:
                    previous = json.loads(previous_path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError):
                    pass
            same_request = isinstance(previous, dict) and all(
                previous.get(key) == incoming.get(key) for key in ("request_id", "stage")
            )
            for source, relative in evidence:
                target = self._target(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            if not same_request:
                response = self._target("response.json")
                if response.exists():
                    archived = self._target(f"history/response-{uuid.uuid4().hex}.json")
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    response.replace(archived)
            # Advertise the new request only after its evidence and cleanup are ready.
            self.upload_request(request_path)
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
            _atomic_json(self._target("request.json"), json.loads(request_path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise BridgeTransportError(f"could not update Drive sync request: {exc}") from exc

    def upload_response(self, response_path: Path) -> None:
        try:
            self.remote.mkdir(parents=True, exist_ok=True)
            shutil.copy2(response_path, self.remote / "response.json")
        except OSError as exc:
            raise BridgeTransportError(f"could not update Drive sync response: {exc}") from exc

    def archive_response(self, request_id: str, revision: int) -> None:
        response = self.remote / "response.json"
        if not response.exists():
            return
        try:
            history = self.remote / "history"
            history.mkdir(parents=True, exist_ok=True)
            target = history / f"response-{request_id}-r{revision}.json"
            if target.exists():
                target = history / f"response-{request_id}-r{revision}-{uuid.uuid4().hex}.json"
            response.replace(target)
        except OSError as exc:
            raise BridgeTransportError(f"could not archive Drive sync response: {exc}") from exc

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

    def archive_response(self, request_id: str, revision: int) -> None:
        folder = self._agent_folder()
        response = self._find_one(folder, "response.json")
        if response is None:
            return
        history = self._find_or_create_folder(folder, "history")
        try:
            self.service.files().update(
                fileId=response["id"],
                body={"name": f"response-{request_id}-r{revision}.json"},
                addParents=history,
                removeParents=folder,
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:  # pragma: no cover - depends on Drive
            raise BridgeTransportError(f"could not archive Drive response: {exc}") from exc

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
        self.agent_state = AgentStateStore(runner.project.agent_state_path)
        self.transport = transport or self._transport_for(config)
        self._resume = resume or (lambda: AgentBridgeService(self.runner).next())
        self._sleep = sleeper

    def publish(self, stage: str) -> dict[str, Any]:
        footage_progress = self._footage_progress() if stage == "footage" else None
        footage_batch = footage_progress.activate() if footage_progress else None
        if (
            footage_progress
            and footage_batch is None
            and footage_progress.remaining_count() > 0
        ):
            raise BridgeError(
                "footage semantic batching has unfinished work but no eligible batch; "
                "inspect .moon/footage-semantic-progress.json"
            )
        state = self._read_state()
        active = state.get("active_request") or {}
        if (
            active.get("stage") == stage
            and active.get("status") == "CONSUMED"
            and (
                footage_batch is None
                or footage_batch.get("request_id") == active.get("request_id")
            )
        ):
            request_id = str(active.get("request_id") or "")
            resume = self._resume_pending(state, request_id)
            return {
                "status": "CONSUMED",
                "idempotent": True,
                "request": self._read_json(self.request_path),
                "resume": resume,
            }
        pending_same_stage = (
            active.get("stage") == stage
            and active.get("status") == "WAITING_AGENT"
            and self.runner.state.next_stage() == stage
        )
        pending_request: dict[str, Any] | None = None
        if pending_same_stage and self.request_path.is_file():
            try:
                pending_request = self._read_json(self.request_path)
            except (OSError, UnicodeError, json.JSONDecodeError, BridgeError):
                pending_request = None
        expired_pending = pending_same_stage and (
            self._active_request_expired(active)
            or pending_request is None
            or self._active_request_expired(pending_request)
        )
        reusable_pending = (
            pending_same_stage
            and not expired_pending
            and pending_request is not None
            and self._request_matches_active(pending_request, active)
            and (
                footage_batch is None
                or (pending_request.get("task") or {}).get("batch_id")
                == footage_batch.get("batch_id")
            )
        )
        if reusable_pending:
            self._ensure_agent_state(state, pending_request)
            self._ensure_portable_packet(pending_request)
            evidence = self._publish_paths(pending_request)
            remote = self.transport.publish(self.request_path, evidence)
            return {
                "status": "WAITING_AGENT",
                "idempotent": True,
                "request": pending_request,
                "remote": remote,
            }

        refreshing_pending = pending_same_stage and not reusable_pending
        if refreshing_pending:
            self._archive_remote_response(active)
        elif (
            footage_batch
            and active.get("stage") == stage
            and active.get("status") == "CONSUMED"
            and footage_batch.get("request_id") != active.get("request_id")
        ):
            self._archive_remote_response(active)

        handoff = AgentHandoffService(self.runner).package(stage)
        request_id = uuid.uuid4().hex
        created = _utc_now()
        expires = created + timedelta(seconds=self.config.stale_after_seconds)
        preserved_revision = int(
            footage_batch.get("revision", self.runner.state.revision)
            if footage_batch
            else self.runner.state.revision
        )
        if refreshing_pending:
            preserved_revision = max(
                int(active.get("revision", self.runner.state.revision)),
                self.runner.state.revision,
            )
        evidence = self._stage_evidence(
            request_id, handoff, handoff_revision=preserved_revision
        )
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
            "expected_response_schema": self._response_schema(
                handoff["output_contract"],
                stage=stage,
                footage_batch=footage_batch is not None,
            ),
        }
        if isinstance(request.get("task"), dict):
            request["task"]["revision"] = preserved_revision
            if footage_batch:
                public_batch = footage_progress.public_batch(footage_batch)
                public_batch.update(
                    request_id=request_id,
                    revision=preserved_revision,
                    status="waiting_agent",
                )
                request["task"].update(
                    batch_id=footage_batch["batch_id"],
                    batch_type=footage_batch["batch_type"],
                    batch_clip_ids=footage_batch["clip_ids"],
                    batch_ranges=public_batch["ranges"],
                )
        request["route"] = self._initial_route(
            request, handoff, revision=preserved_revision
        )
        if footage_batch:
            request["route"]["batch"] = public_batch
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_portable_packet(request)
        self._archive_previous_response(state)
        _atomic_json(self.request_path, request)
        if refreshing_pending:
            expired = state.get("expired") or {}
            expired_request_id = str(active.get("request_id") or "unknown")
            expired[expired_request_id] = {
                "job_id": active.get("job_id"),
                "stage": active.get("stage"),
                "revision": preserved_revision,
                "expired_at": active.get("expires_at"),
                "reason": "expired" if expired_pending else "pending_request_invalid",
                "replaced_at": request["created_at"],
                "replacement_request_id": request_id,
            }
            state["expired"] = dict(list(expired.items())[-100:])
        state["active_request"] = {
            "job_id": self.config.project_id,
            "request_id": request_id,
            "stage": stage,
            "status": "WAITING_AGENT",
            "created_at": request["created_at"],
            "expires_at": request["expires_at"],
            "revision": request["route"]["revision"],
            "batch_id": footage_batch.get("batch_id") if footage_batch else None,
        }
        if footage_batch:
            footage_progress.bind_request(
                footage_batch["batch_id"],
                request_id,
                preserved_revision,
                [str(item[2].get("sha256") or "") for item in evidence],
            )
        self._write_state(state)
        self.agent_state.save(request["route"])
        remote = self.transport.publish(self.request_path, self._publish_paths(request))
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
                warnings: list[str] = []
                if resume and resume.get("status") != "resume_pending":
                    try:
                        self.transport.upload_request(self.request_path)
                    except BridgeTransportError as exc:
                        warnings.append(str(exc))
                return {
                    "status": "CONSUMED" if resume and resume.get("status") != "resume_pending" else "CONSUMED_RESUME_PENDING",
                    "request_id": request_id,
                    "stage": consumed[request_id].get("stage"),
                    "submission": {"accepted": False, "duplicate": True},
                    "resume": resume,
                    "warnings": warnings,
                }
            raise DuplicateResponseError(
                f"response for request_id {request_id!r} was already consumed"
            )
        self._validate_response(response, active)
        review = response.get("review") if isinstance(response.get("review"), dict) else None
        if response["stage"] == "analyze" and review:
            self._validate_analyze_review(review, active)
            if review["decision"] == "REVISION_REQUIRED":
                return self._route_analyze_revision(response, raw, state, active, review)
        if response["stage"] == "footage" and review:
            self._validate_footage_review(response, review, active)
            if review["decision"] == "REQUEST_REFINEMENT":
                progress = FootageSemanticProgress.open_existing(self.runner)
                if progress and progress.batch_for_request(str(request_id)):
                    return self._route_footage_batch_refinement(
                        response, raw, state, active, progress
                    )
                return self._route_footage_refinement(
                    response, raw, state, active
                )
        routing_review = review
        if response["stage"] in {"analyze", "footage"} and routing_review is None:
            # Responses produced before the routed protocol remain consumable.
            # Record the implied GPT approval so the durable state machine is complete.
            routing_review = {
                "actor": "gpt",
                "decision": "APPROVED",
                "revision": int(active.get("revision", self.runner.state.revision)),
            }
        response_hash = _sha256_bytes(raw)
        footage_progress = (
            FootageSemanticProgress.open_existing(self.runner)
            if response["stage"] == "footage"
            else None
        )
        footage_batch = (
            footage_progress.batch_for_request(str(request_id))
            if footage_progress
            else None
        )
        if footage_batch:
            try:
                outcome = footage_progress.complete(
                    str(request_id), response["payload"], response_hash
                )
            except ValueError as exc:
                raise BridgeResponseError(str(exc)) from exc
            final_payload = outcome.get("final_payload")
            if final_payload is not None:
                submission = AgentHandoffService(self.runner).submit(
                    "footage", final_payload
                )
                submission["assembled_batches"] = True
            else:
                submission = {
                    "accepted": True,
                    "stage": "footage",
                    "artifact": "footage_semantic_batch",
                    "batch": outcome["batch"],
                    "remaining_batches": outcome["remaining_batches"],
                }
        else:
            submission = AgentHandoffService(self.runner).submit(
                response["stage"], response["payload"]
            )
        return self._finalize_consumed_response(
            response,
            response_hash=response_hash,
            state=state,
            active=active,
            routing_review=routing_review,
            submission=submission,
        )

    def _finalize_consumed_response(
        self,
        response: dict[str, Any],
        *,
        response_hash: str,
        state: dict[str, Any],
        active: dict[str, Any],
        routing_review: dict[str, Any] | None,
        submission: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(response["request_id"])
        consumed_at = _iso(_utc_now())
        consumed_response = dict(response)
        consumed_response["status"] = "CONSUMED"
        consumed_response["consumed_at"] = consumed_at
        consumed_response["updated_at"] = consumed_at
        _atomic_json(self.response_path, consumed_response)
        request = self._read_json(self.request_path)
        route = self._route_to_moon(request.get("route"), review=routing_review)
        request["status"] = "CONSUMED"
        request["route"] = route
        request["updated_at"] = consumed_at
        _atomic_json(self.request_path, request)
        self.agent_state.save(route)
        consumed = state.get("consumed") or {}
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
        if resume and resume.get("status") != "resume_pending":
            self._mark_moon_continue()
            try:
                self.transport.upload_request(self.request_path)
            except BridgeTransportError as exc:
                sync_warnings.append(str(exc))
        result = {
            "status": "CONSUMED" if resume and resume.get("status") != "resume_pending" else "CONSUMED_RESUME_PENDING",
            "request_id": request_id,
            "stage": response["stage"],
            "submission": submission,
            "resume": resume,
            "warnings": sync_warnings,
        }
        if "remaining_batches" in submission:
            result["remaining_batches"] = submission["remaining_batches"]
        return result

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
        route = self._ensure_agent_state(state)
        active = state.get("active_request") or {}
        request_expired = (
            active.get("status") == "WAITING_AGENT"
            and self._active_request_expired(active)
        )
        if not request_expired and active.get("status") == "WAITING_AGENT" and self.request_path.is_file():
            try:
                request_expired = self._active_request_expired(
                    self._read_json(self.request_path)
                )
            except (OSError, UnicodeError, json.JSONDecodeError, BridgeError):
                request_expired = True
        return {
            "job_id": self.config.project_id,
            "local_agent_dir": str(self.agent_dir),
            "remote_path": self.config.remote_path,
            "active_request": state.get("active_request"),
            "request_expired": request_expired,
            "request_lifecycle": "expired" if request_expired else (
                "fresh" if active.get("status") == "WAITING_AGENT" else active.get("status")
            ),
            "consumed_count": len(state.get("consumed") or {}),
            "remote": self.transport.status(),
            "current_actor": route.get("current_actor") if route else None,
            "next_actor": route.get("next_actor") if route else None,
            "next_action": route.get("next_action") if route else None,
            "agent_state": route,
        }

    def _footage_progress(self) -> FootageSemanticProgress:
        return FootageSemanticProgress(
            self.runner,
            FootageBatchPolicy(
                max_clips=self.config.max_footage_batch_clips,
                max_refinement_ranges=self.config.max_footage_batch_ranges,
                max_frames=min(
                    self.config.max_footage_batch_frames,
                    max(1, self.config.max_evidence_files - 2),
                ),
                max_evidence_bytes=min(
                    self.config.max_footage_batch_bytes,
                    self.config.max_evidence_bytes,
                ),
            ),
        )

    def _resume_pending(self, state: dict[str, Any], request_id: str) -> dict[str, Any] | None:
        entry = (state.get("consumed") or {}).get(request_id)
        if not entry or not entry.get("resume_pending"):
            return None
        stage = str(entry.get("stage") or "")
        if self.runner.state.next_stage() != stage:
            entry["resume_pending"] = False
            entry["resume_result"] = {"status": "already_advanced", "pipeline": self.runner.status()}
            self._write_state(state)
            self._mark_moon_continue()
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
        self._mark_moon_continue()
        return result

    def _initial_route(
        self,
        request: dict[str, Any],
        handoff: dict[str, Any],
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        stage = request["stage"]
        artifact = str(handoff["output_contract"].get("artifact") or "stage_payload")
        if revision is None:
            revision = int(
                handoff.get("pipeline", {}).get("revision", self.runner.state.revision)
            )
        required_inputs = [str(item["path"]) for item in request.get("evidence") or []]
        instruction = str(request.get("task", {}).get("instruction") or f"Complete {stage}.")
        if stage == "analyze":
            ack = self._acknowledgement(
                request, actor="gemini", decision="COMPLETED", output=artifact,
                next_actor="gpt", next_action="REVIEW_GEMINI_ANALYSIS", revision=revision,
            )
            state = {
                "version": "1.0",
                "job_id": request["job_id"],
                "stage": stage,
                "revision": revision,
                "request_id": request["request_id"],
                "status": "MOON_PREPARE",
                "current_actor": "moon",
                "next_actor": "gemini",
                "next_action": "ANALYZE_EVIDENCE",
                "task": instruction,
                "required_inputs": required_inputs,
                "expected_output": {
                    "artifact": artifact,
                    "delivery": "Return the artifact in chat; do not write raw JSON to Drive.",
                    "schema_ref": "request.json.expected_response_schema.properties.payload",
                },
                "completion_contract": {
                    "terminal_acknowledgement": ack,
                    "gemini_return": {
                        "required": [artifact, "terminal_acknowledgement"],
                        "handoff": "Give the complete Gemini result and acknowledgement to GPT.",
                    },
                    "gpt_review": {
                        "input": "The pasted Gemini result plus this Drive request and evidence.",
                        "output_file": "response.json",
                        "decisions": ["APPROVED", "REVISION_REQUIRED"],
                        "acknowledgements": {
                            "APPROVED": self._acknowledgement(
                                request, actor="gpt", decision="APPROVED",
                                output="response.json", next_actor="moon",
                                next_action="CONSUME_RESPONSE", revision=revision,
                            ),
                            "REVISION_REQUIRED": self._acknowledgement(
                                request, actor="gpt", decision="REVISION_REQUIRED",
                                output="revision_targets", next_actor="gemini",
                                next_action="RECHECK_TARGETS", revision=revision,
                            ),
                        },
                    },
                    "on_approval": {
                        "next_actor": "moon",
                        "next_action": "CONSUME_RESPONSE",
                    },
                    "on_revision": {
                        "next_actor": "gemini",
                        "next_action": "RECHECK_TARGETS",
                        "required_metadata": ["segment_id", "reason"],
                    },
                },
                "actor_instructions": {
                    "gemini": "Read every required input, return the semantic enrichment in chat, then end with the exact terminal acknowledgement. If direct Drive reading is unavailable, ask the user to upload the single gemini_handoff.pdf packet instead of pasting files.",
                    "gpt": "Review the pasted Gemini result against the Drive evidence. Write response.json only after recording APPROVED or REVISION_REQUIRED in review.decision.",
                },
                "portable_packet": "gemini_handoff.pdf",
                "updated_at": utc_now(),
                "transition_history": [],
            }
            state = transition(
                state, "MOON_PREPARE", current_actor="moon", next_actor="gemini",
                next_action="ANALYZE_EVIDENCE",
            )
            return transition(
                state, "WAITING_GEMINI", current_actor="gemini", next_actor="gpt",
                next_action="REVIEW_GEMINI_ANALYSIS",
            )
        if stage == "footage":
            batch = (request.get("task") or {}).get("batch_id")
            if batch:
                artifact = "footage_semantic_batch"
            completed_ack = self._acknowledgement(
                request, actor="gemini", decision="COMPLETED", output=artifact,
                next_actor="gpt", next_action="REVIEW_GEMINI_FOOTAGE",
                revision=revision,
            )
            refinement_ack = self._acknowledgement(
                request, actor="gemini", decision="REQUEST_REFINEMENT",
                output="footage_refinement_request", next_actor="gpt",
                next_action="REVIEW_GEMINI_FOOTAGE", revision=revision,
            )
            state = {
                "version": "1.0",
                "job_id": request["job_id"],
                "stage": stage,
                "revision": revision,
                "request_id": request["request_id"],
                "status": "MOON_PREPARE",
                "current_actor": "moon",
                "next_actor": "gemini",
                "next_action": "ANALYZE_FOOTAGE_EVIDENCE",
                "task": instruction,
                "required_inputs": required_inputs,
                "expected_output": {
                    "artifact": artifact,
                    "alternate_artifact": "footage_refinement_request",
                    "delivery": "Return one complete artifact in chat; do not write raw JSON to Drive.",
                    "schema_ref": "request.json.expected_response_schema.properties.payload",
                },
                "completion_contract": {
                    "terminal_acknowledgement": completed_ack,
                    "gemini_return": {
                        "required": [
                            "footage_semantic_enrichment or footage_refinement_request",
                            "terminal_acknowledgement",
                        ],
                        "acknowledgements": {
                            "COMPLETED": completed_ack,
                            "REQUEST_REFINEMENT": refinement_ack,
                        },
                        "handoff": "Give the complete Gemini result and acknowledgement to GPT.",
                    },
                    "gpt_review": {
                        "input": "The pasted Gemini result plus this request and evidence.",
                        "output_file": "response.json",
                        "decisions": ["APPROVED", "REQUEST_REFINEMENT"],
                        "acknowledgements": {
                            "APPROVED": self._acknowledgement(
                                request, actor="gpt", decision="APPROVED",
                                output="response.json", next_actor="moon",
                                next_action="CONSUME_RESPONSE", revision=revision,
                            ),
                            "REQUEST_REFINEMENT": self._acknowledgement(
                                request, actor="gpt", decision="REQUEST_REFINEMENT",
                                output="footage_refinement_request", next_actor="moon",
                                next_action="REQUEST_REFINEMENT", revision=revision,
                            ),
                        },
                    },
                    "on_approval": {
                        "next_actor": "moon",
                        "next_action": "CONSUME_RESPONSE",
                    },
                    "on_refinement": {
                        "next_actor": "moon",
                        "next_action": "REQUEST_REFINEMENT",
                        "required_metadata": [
                            "clip_id", "start_seconds", "end_seconds", "reason"
                        ],
                    },
                },
                "actor_instructions": {
                    "gemini": "Read only the active batch inputs and sampled frames. Return semantic segments only for the listed batch clips/ranges; do not scan other Drive evidence. When the compact scaffold lists completed_refinement_results, treat those as already accepted partial semantics and do not duplicate or overlap their segments. Never execute Moon or local commands. If a boundary is ambiguous, return the strict footage_refinement_request so Moon can sample narrower windows. Otherwise return the active footage_semantic_batch. End with the matching exact acknowledgement. If direct Drive reading is unavailable, ask the user to upload only gemini_handoff.pdf.",
                    "gpt": "Review the pasted Gemini result against only the active batch request and evidence. Write response.json with review.decision APPROVED for a valid footage_semantic_batch, or REQUEST_REFINEMENT with the strict footage_refinement_request payload. Preserve batch_id when present. Never ask Gemini to execute local sampling commands.",
                },
                "portable_packet": "gemini_handoff.pdf",
                "updated_at": utc_now(),
                "transition_history": [],
            }
            state = transition(
                state, "MOON_PREPARE", current_actor="moon", next_actor="gemini",
                next_action="ANALYZE_FOOTAGE_EVIDENCE",
            )
            return transition(
                state, "WAITING_GEMINI", current_actor="gemini", next_actor="gpt",
                next_action="REVIEW_GEMINI_FOOTAGE",
            )
        ack = self._acknowledgement(
            request, actor="gpt", decision="COMPLETED", output=artifact,
            next_actor="moon", next_action="CONSUME_RESPONSE", revision=revision,
        )
        state = {
            "version": "1.0",
            "job_id": request["job_id"],
            "stage": stage,
            "revision": revision,
            "request_id": request["request_id"],
            "status": "MOON_PREPARE",
            "current_actor": "moon",
            "next_actor": "gpt",
            "next_action": f"COMPLETE_{stage.upper()}",
            "task": instruction,
            "required_inputs": required_inputs,
            "expected_output": {
                "artifact": artifact,
                "delivery": "Write response.json in the Drive AGENT folder.",
                "schema_ref": "request.json.expected_response_schema",
            },
            "completion_contract": {
                "terminal_acknowledgement": ack,
                "on_approval": {"next_actor": "moon", "next_action": "CONSUME_RESPONSE"},
                "on_revision": {"next_actor": "gpt", "next_action": f"REVISE_{stage.upper()}"},
            },
            "updated_at": utc_now(),
            "transition_history": [],
        }
        state = transition(
            state, "MOON_PREPARE", current_actor="moon", next_actor="gpt",
            next_action=f"COMPLETE_{stage.upper()}",
        )
        return transition(
            state, "WAITING_GPT", current_actor="gpt", next_actor="moon",
            next_action="CONSUME_RESPONSE",
        )

    @staticmethod
    def _acknowledgement(
        request: dict[str, Any], *, actor: str, decision: str, output: str,
        next_actor: str, next_action: str, revision: int,
    ) -> str:
        return (
            f"TASK_COMPLETED job_id={request['job_id']} request_id={request['request_id']} "
            f"stage={request['stage']} revision={revision} actor={actor} decision={decision} "
            f"output={output} next_actor={next_actor} next_action={next_action}"
        )

    def _validate_analyze_review(
        self, review: dict[str, Any], active: dict[str, Any]
    ) -> None:
        allowed = {
            "actor", "decision", "revision", "revision_targets", "gemini_acknowledgement"
        }
        unknown = sorted(set(review) - allowed)
        if unknown:
            raise BridgeResponseError(
                f"analyze review contains unsupported fields: {', '.join(unknown)}"
            )
        if review.get("actor", "gpt") != "gpt":
            raise BridgeResponseError("analyze review actor must be gpt")
        if review.get("decision") not in {"APPROVED", "REVISION_REQUIRED"}:
            raise BridgeResponseError("analyze review decision must be APPROVED or REVISION_REQUIRED")
        revision = review.get("revision", active.get("revision", self.runner.state.revision))
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise BridgeResponseError("analyze review revision must be an integer")
        if revision != int(active.get("revision", self.runner.state.revision)):
            raise BridgeResponseError("analyze review revision does not match active request")
        if review["decision"] == "REVISION_REQUIRED":
            targets = review.get("revision_targets")
            if not isinstance(targets, list) or not targets:
                raise BridgeResponseError("REVISION_REQUIRED needs non-empty revision_targets")
            valid_segment_ids: set[str] = set()
            if self.runner.artifacts.exists("reference_blueprint_scaffold"):
                scaffold = self.runner.artifacts.read("reference_blueprint_scaffold")
                valid_segment_ids = {
                    str(segment.get("id"))
                    for segment in scaffold.get("segments") or []
                    if isinstance(segment, dict) and segment.get("id")
                }
            for target in targets:
                if not isinstance(target, dict) or set(target) != {"segment_id", "reason"}:
                    raise BridgeResponseError("each revision target requires only segment_id and reason")
                segment_id = str(target.get("segment_id") or "").strip()
                if not segment_id or not str(target.get("reason") or "").strip():
                    raise BridgeResponseError("each revision target requires segment_id and reason")
                if valid_segment_ids and segment_id not in valid_segment_ids:
                    raise BridgeResponseError(
                        f"revision target segment_id {segment_id!r} is not in the active analyze scaffold"
                    )

    def _validate_footage_review(
        self,
        response: dict[str, Any],
        review: dict[str, Any],
        active: dict[str, Any],
    ) -> None:
        allowed = {"actor", "decision", "revision", "gemini_acknowledgement"}
        unknown = sorted(set(review) - allowed)
        if unknown:
            raise BridgeResponseError(
                f"footage review contains unsupported fields: {', '.join(unknown)}"
            )
        if review.get("actor", "gpt") != "gpt":
            raise BridgeResponseError("footage review actor must be gpt")
        if review.get("decision") not in {"APPROVED", "REQUEST_REFINEMENT"}:
            raise BridgeResponseError(
                "footage review decision must be APPROVED or REQUEST_REFINEMENT"
            )
        revision = review.get(
            "revision", active.get("revision", self.runner.state.revision)
        )
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise BridgeResponseError("footage review revision must be an integer")
        if revision != int(active.get("revision", self.runner.state.revision)):
            raise BridgeResponseError(
                "footage review revision does not match active request"
            )
        if review["decision"] == "REQUEST_REFINEMENT":
            try:
                FootageRefinementService(self.runner).validate(response["payload"])
            except (OSError, ValueError) as exc:
                raise BridgeResponseError(str(exc)) from exc
        elif response["payload"].get("artifact") == "footage_refinement_request":
            raise BridgeResponseError(
                "footage_refinement_request requires review.decision=REQUEST_REFINEMENT"
            )

    def _route_footage_refinement(
        self,
        response: dict[str, Any],
        raw: bytes,
        bridge_state: dict[str, Any],
        active: dict[str, Any],
    ) -> dict[str, Any]:
        request = self._read_json(self.request_path)
        route = self._ensure_agent_state(bridge_state, request) or request["route"]
        old_revision = int(route.get("revision", 0))
        route = transition(
            route,
            "GEMINI_FOOTAGE_RECHECK_DONE" if old_revision else "GEMINI_FOOTAGE_DONE",
            current_actor="gemini",
            next_actor="gpt",
            next_action="REVIEW_GEMINI_FOOTAGE",
        )
        route = transition(
            route, "WAITING_GPT", current_actor="gpt", next_actor="moon",
            next_action="REQUEST_REFINEMENT",
        )
        route = transition(
            route, "REQUEST_REFINEMENT", current_actor="gpt", next_actor="moon",
            next_action="REQUEST_REFINEMENT",
        )
        service = FootageRefinementService(self.runner)
        try:
            targets = service.validate(response["payload"])
            new_revision = old_revision + 1
            sampling = service.sample(targets, handoff_revision=new_revision)
        except (OSError, ValueError) as exc:
            raise BridgeResponseError(f"could not satisfy footage refinement: {exc}") from exc

        handoff = AgentHandoffService(self.runner).package("footage")
        evidence = self._stage_evidence(
            response["request_id"], handoff, handoff_revision=new_revision
        )
        route["revision"] = new_revision
        route["refinement_targets"] = targets
        route["refinement_sampling"] = sampling
        route["required_inputs"] = [relative for _, relative, _ in evidence]
        route["task"] = (
            "Recheck only the listed footage refinement targets using their newly "
            "sampled measured frames. Return another footage_refinement_request only "
            "if a listed boundary remains ambiguous; otherwise return a complete "
            "footage_semantic_enrichment. Never execute Moon or local commands."
        )
        route["completion_contract"] = dict(route["completion_contract"])
        completed_ack = self._acknowledgement(
            request, actor="gemini", decision="RECHECK_COMPLETED",
            output=str(route["expected_output"]["artifact"]), next_actor="gpt",
            next_action="REVIEW_GEMINI_FOOTAGE", revision=new_revision,
        )
        refinement_ack = self._acknowledgement(
            request, actor="gemini", decision="REQUEST_REFINEMENT",
            output="footage_refinement_request", next_actor="gpt",
            next_action="REVIEW_GEMINI_FOOTAGE", revision=new_revision,
        )
        route["completion_contract"]["terminal_acknowledgement"] = completed_ack
        gemini_return = dict(
            route["completion_contract"].get("gemini_return") or {}
        )
        gemini_return["acknowledgements"] = {
            "COMPLETED": completed_ack,
            "REQUEST_REFINEMENT": refinement_ack,
        }
        route["completion_contract"]["gemini_return"] = gemini_return
        gpt_review = dict(route["completion_contract"].get("gpt_review") or {})
        gpt_review["acknowledgements"] = {
            "APPROVED": self._acknowledgement(
                request, actor="gpt", decision="APPROVED", output="response.json",
                next_actor="moon", next_action="CONSUME_RESPONSE",
                revision=new_revision,
            ),
            "REQUEST_REFINEMENT": self._acknowledgement(
                request, actor="gpt", decision="REQUEST_REFINEMENT",
                output="footage_refinement_request", next_actor="moon",
                next_action="REQUEST_REFINEMENT", revision=new_revision,
            ),
        }
        route["completion_contract"]["gpt_review"] = gpt_review
        route = transition(
            route, "RECHECK_TARGETS", current_actor="moon", next_actor="gemini",
            next_action="RECHECK_TARGETS",
        )
        route = transition(
            route, "WAITING_GEMINI", current_actor="gemini", next_actor="gpt",
            next_action="REVIEW_GEMINI_FOOTAGE",
        )
        now = _utc_now()
        request.update(
            status="WAITING_AGENT",
            route=route,
            evidence=[descriptor for _, _, descriptor in evidence],
            updated_at=_iso(now),
            created_at=_iso(now),
            expires_at=_iso(
                now + timedelta(seconds=self.config.stale_after_seconds)
            ),
        )
        if isinstance(request.get("task"), dict):
            request["task"]["revision"] = new_revision
        self._ensure_portable_packet(request)
        archive = getattr(self.transport, "archive_response", None)
        if callable(archive):
            archive(response["request_id"], old_revision)
        elif hasattr(self.transport, "response"):
            self.transport.response = None
        active.update(
            status="WAITING_AGENT",
            revision=new_revision,
            created_at=request["created_at"],
            expires_at=request["expires_at"],
        )
        reviewed = bridge_state.get("reviewed") or {}
        reviewed[f"{response['request_id']}:r{old_revision}"] = {
            "response_sha256": _sha256_bytes(raw),
            "decision": "REQUEST_REFINEMENT",
            "refinement_targets": targets,
            "sampling": sampling,
            "reviewed_at": _iso(now),
        }
        bridge_state["reviewed"] = dict(list(reviewed.items())[-100:])
        bridge_state["active_request"] = active
        _atomic_json(self.request_path, request)
        self.agent_state.save(route)
        self._write_state(bridge_state)
        self._archive_local_review(response, old_revision)
        remote = self.transport.publish(
            self.request_path, self._publish_paths(request)
        )
        return {
            "status": "WAITING_GEMINI",
            "request_id": response["request_id"],
            "stage": "footage",
            "revision": new_revision,
            "refinement_targets": targets,
            "sampling": sampling,
            "next_actor": "gemini",
            "next_action": "RECHECK_TARGETS",
            "remote": remote,
        }

    def _route_footage_batch_refinement(
        self,
        response: dict[str, Any],
        raw: bytes,
        bridge_state: dict[str, Any],
        active: dict[str, Any],
        progress: FootageSemanticProgress,
    ) -> dict[str, Any]:
        service = FootageRefinementService(self.runner)
        response_hash = _sha256_bytes(raw)
        try:
            targets = service.validate(response["payload"])
            replayed = progress.replayed_refinement(
                str(response["request_id"]), response_hash
            )
            if replayed is not None:
                submission = {
                    "accepted": True,
                    "stage": "footage",
                    "artifact": "footage_refinement_request",
                    "targets": targets,
                    "sampling": {
                        "sampled_groups": 0,
                        "skipped_existing_groups": len(replayed["group_ids"]),
                        "groups": replayed["group_ids"],
                        "checkpoint_replay": True,
                    },
                    **replayed,
                }
                result = self._finalize_consumed_response(
                    response,
                    response_hash=response_hash,
                    state=bridge_state,
                    active=active,
                    routing_review=response.get("review"),
                    submission=submission,
                )
                result.update(
                    next_action="NEXT_FOOTAGE_BATCH",
                    remaining_batches=progress.remaining_count(),
                )
                return result
            progress_value = progress.load()
            sampling_revision = int(
                progress_value.get(
                    "next_revision", int(active.get("revision", 0)) + 1
                )
            )
            sampling = service.sample(
                targets, handoff_revision=sampling_revision
            )
            deferred = progress.defer_for_refinement(
                str(response["request_id"]),
                [str(item["group_id"]) for item in sampling["groups"]],
                response_hash,
            )
        except (OSError, ValueError) as exc:
            raise BridgeResponseError(f"could not satisfy footage refinement: {exc}") from exc
        submission = {
            "accepted": True,
            "stage": "footage",
            "artifact": "footage_refinement_request",
            "targets": targets,
            "sampling": sampling,
            **deferred,
        }
        result = self._finalize_consumed_response(
            response,
            response_hash=response_hash,
            state=bridge_state,
            active=active,
            routing_review=response.get("review"),
            submission=submission,
        )
        result.update(
            next_action="NEXT_FOOTAGE_BATCH",
            remaining_batches=progress.remaining_count(),
        )
        return result

    def _route_analyze_revision(
        self,
        response: dict[str, Any],
        raw: bytes,
        bridge_state: dict[str, Any],
        active: dict[str, Any],
        review: dict[str, Any],
    ) -> dict[str, Any]:
        request = self._read_json(self.request_path)
        route = self._ensure_agent_state(bridge_state, request) or request["route"]
        done_status = "GEMINI_RECHECK_DONE" if int(route.get("revision", 0)) else "GEMINI_DONE"
        route = transition(
            route, done_status, current_actor="gemini", next_actor="gpt",
            next_action="REVIEW_GEMINI_ANALYSIS",
        )
        route = transition(
            route, "WAITING_GPT", current_actor="gpt", next_actor="moon",
            next_action="CONSUME_RESPONSE",
        )
        route = transition(
            route, "GPT_REVISION_REQUIRED", current_actor="gpt", next_actor="gemini",
            next_action="RECHECK_TARGETS",
        )
        old_revision = int(route.get("revision", 0))
        new_revision = old_revision + 1
        targets = [
            {"segment_id": str(item["segment_id"]), "reason": str(item["reason"])}
            for item in review["revision_targets"]
        ]
        route["revision"] = new_revision
        route["revision_targets"] = targets
        route["task"] = "Recheck only the listed target segments against the published evidence and return a corrected complete semantic_enrichment artifact."
        route["completion_contract"] = dict(route["completion_contract"])
        route["completion_contract"]["terminal_acknowledgement"] = self._acknowledgement(
            request, actor="gemini", decision="RECHECK_COMPLETED",
            output=str(route["expected_output"]["artifact"]), next_actor="gpt",
            next_action="REVIEW_GEMINI_ANALYSIS", revision=new_revision,
        )
        gpt_review = dict(route["completion_contract"].get("gpt_review") or {})
        gpt_review["acknowledgements"] = {
            "APPROVED": self._acknowledgement(
                request, actor="gpt", decision="APPROVED", output="response.json",
                next_actor="moon", next_action="CONSUME_RESPONSE", revision=new_revision,
            ),
            "REVISION_REQUIRED": self._acknowledgement(
                request, actor="gpt", decision="REVISION_REQUIRED",
                output="revision_targets", next_actor="gemini",
                next_action="RECHECK_TARGETS", revision=new_revision,
            ),
        }
        route["completion_contract"]["gpt_review"] = gpt_review
        route = transition(
            route, "WAITING_GEMINI", current_actor="gemini", next_actor="gpt",
            next_action="REVIEW_GEMINI_ANALYSIS",
        )
        now = _utc_now()
        request.update(
            status="WAITING_AGENT", route=route, updated_at=_iso(now),
            created_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=self.config.stale_after_seconds)),
        )
        if isinstance(request.get("task"), dict):
            request["task"]["revision"] = new_revision
        self._ensure_portable_packet(request)
        active.update(
            status="WAITING_AGENT", revision=new_revision,
            created_at=request["created_at"], expires_at=request["expires_at"],
        )
        reviewed = bridge_state.get("reviewed") or {}
        reviewed[f"{response['request_id']}:r{old_revision}"] = {
            "response_sha256": _sha256_bytes(raw),
            "decision": "REVISION_REQUIRED",
            "revision_targets": targets,
            "reviewed_at": _iso(now),
        }
        bridge_state["reviewed"] = dict(list(reviewed.items())[-100:])
        bridge_state["active_request"] = active
        _atomic_json(self.request_path, request)
        self.agent_state.save(route)
        self._write_state(bridge_state)
        self._archive_local_review(response, old_revision)
        archive = getattr(self.transport, "archive_response", None)
        if callable(archive):
            archive(response["request_id"], old_revision)
        elif hasattr(self.transport, "response"):
            self.transport.response = None
        remote = self.transport.publish(self.request_path, self._publish_paths(request))
        return {
            "status": "WAITING_GEMINI",
            "request_id": response["request_id"],
            "stage": "analyze",
            "revision": new_revision,
            "revision_targets": targets,
            "next_actor": "gemini",
            "next_action": "RECHECK_TARGETS",
            "remote": remote,
        }

    def _route_to_moon(
        self, route_value: Any, *, review: dict[str, Any] | None
    ) -> dict[str, Any]:
        route = dict(route_value) if isinstance(route_value, dict) else {}
        if route.get("stage") in {"analyze", "footage"} and review:
            if route.get("stage") == "analyze":
                done_status = (
                    "GEMINI_RECHECK_DONE"
                    if int(route.get("revision", 0))
                    else "GEMINI_DONE"
                )
                review_action = "REVIEW_GEMINI_ANALYSIS"
            else:
                done_status = (
                    "GEMINI_FOOTAGE_RECHECK_DONE"
                    if int(route.get("revision", 0))
                    else "GEMINI_FOOTAGE_DONE"
                )
                review_action = "REVIEW_GEMINI_FOOTAGE"
            route = transition(
                route, done_status, current_actor="gemini", next_actor="gpt",
                next_action=review_action,
            )
            route = transition(
                route, "WAITING_GPT", current_actor="gpt", next_actor="moon",
                next_action="CONSUME_RESPONSE",
            )
            if (
                route.get("stage") == "footage"
                and review.get("decision") == "REQUEST_REFINEMENT"
            ):
                route = transition(
                    route, "REQUEST_REFINEMENT", current_actor="gpt",
                    next_actor="moon", next_action="REQUEST_REFINEMENT",
                )
            else:
                route = transition(
                    route, "GPT_APPROVED", current_actor="gpt", next_actor="moon",
                    next_action="CONSUME_RESPONSE",
                )
        return transition(
            route, "WAITING_MOON", current_actor="moon", next_actor="moon",
            next_action="CONSUME_RESPONSE",
        )

    def _mark_moon_continue(self) -> None:
        if not self.request_path.is_file():
            return
        request = self._read_json(self.request_path)
        route = request.get("route")
        if not isinstance(route, dict) or route.get("status") == "MOON_CONTINUE":
            return
        route = transition(
            route, "MOON_CONTINUE", current_actor="moon", next_actor="moon",
            next_action="ADVANCE_PIPELINE",
        )
        request["route"] = route
        request["updated_at"] = _iso(_utc_now())
        _atomic_json(self.request_path, request)
        self.agent_state.save(route)

    def _ensure_agent_state(
        self, bridge_state: dict[str, Any], request: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        if request is None and self.request_path.is_file():
            try:
                request = self._read_json(self.request_path)
            except (OSError, ValueError, json.JSONDecodeError):
                request = None
        active = bridge_state.get("active_request") or {}
        published = request.get("route") if isinstance(request, dict) else None
        local = self.agent_state.load()
        expected_identity = (
            active.get("job_id"), active.get("request_id"), active.get("stage"),
            int(active.get("revision", self.runner.state.revision)),
        )

        def identity(value: dict[str, Any]) -> tuple[Any, Any, Any, int]:
            return (
                value.get("job_id"), value.get("request_id"), value.get("stage"),
                int(value.get("revision", self.runner.state.revision)),
            )

        if isinstance(published, dict) and identity(published) == expected_identity:
            if local != published:
                local = self.agent_state.save(published)
            return local
        if local is not None and identity(local) == expected_identity:
            return local
        if isinstance(request, dict) and all(
            request.get(field) == active.get(field) for field in ("job_id", "request_id", "stage")
        ):
            output_contract = (
                (request.get("expected_response_schema") or {}).get("properties", {}).get("payload")
                or {"artifact": "stage_payload"}
            )
            reconstructed = self._initial_route(
                request,
                {
                    "output_contract": output_contract,
                    "pipeline": {"revision": expected_identity[3]},
                },
            )
            request["route"] = reconstructed
            active["revision"] = reconstructed["revision"]
            bridge_state["active_request"] = active
            _atomic_json(self.request_path, request)
            self._write_state(bridge_state)
            return self.agent_state.save(reconstructed)
        return None

    def _archive_local_review(self, response: dict[str, Any], revision: int) -> None:
        history = self.runner.project.moon_dir / "bridge-history"
        history.mkdir(parents=True, exist_ok=True)
        target = history / f"{response['request_id']}-review-r{revision}.json"
        _atomic_json(target, response)

    @staticmethod
    def _request_matches_active(
        request: dict[str, Any], active: dict[str, Any]
    ) -> bool:
        return all(
            request.get(field) == active.get(field)
            for field in ("job_id", "request_id", "stage", "status")
        )

    @staticmethod
    def _active_request_expired(
        active: dict[str, Any], *, now: datetime | None = None
    ) -> bool:
        value = active.get("expires_at")
        if not isinstance(value, str) or not value.strip():
            return True
        try:
            expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
        if expires.tzinfo is None:
            return True
        return (now or _utc_now()) >= expires.astimezone(timezone.utc)

    def _archive_remote_response(self, active: dict[str, Any]) -> None:
        archive = getattr(self.transport, "archive_response", None)
        if callable(archive):
            archive(
                str(active.get("request_id") or "unknown"),
                int(active.get("revision", self.runner.state.revision)),
            )
        elif hasattr(self.transport, "response"):
            self.transport.response = None

    def _ensure_portable_packet(self, request: dict[str, Any]) -> Path | None:
        if request.get("stage") not in {"analyze", "footage"}:
            return None
        route = request.get("route")
        if not isinstance(route, dict):
            raise BridgeError("visual request route is missing")
        route["portable_packet"] = "gemini_handoff.pdf"
        packet_path = self.agent_dir / "gemini_handoff.pdf"
        packet = route.get("portable_packet_manifest")
        if (
            isinstance(packet, dict)
            and packet.get("request_id") == request.get("request_id")
            and packet.get("stage") == request.get("stage")
            and packet.get("revision") == route.get("revision")
            and packet_path.is_file()
            and _sha256(packet_path) == packet.get("sha256")
        ):
            return packet_path
        try:
            packet = GeminiHandoffPacketBuilder(self.agent_dir).build(
                request, route, packet_path
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeError(f"could not build Gemini handoff packet: {exc}") from exc
        route["portable_packet_manifest"] = packet
        if self.request_path.is_file() and self._request_matches_active(
            request, (self._read_state().get("active_request") or {})
        ):
            _atomic_json(self.request_path, request)
            self.agent_state.save(route)
        return packet_path

    def _publish_paths(self, request: dict[str, Any]) -> list[tuple[Path, str]]:
        paths = self._evidence_paths(request)
        if request.get("stage") not in {"analyze", "footage"}:
            return paths
        route = request.get("route") or {}
        packet = route.get("portable_packet_manifest") or {}
        relative = str(route.get("portable_packet") or "")
        packet_path = (self.agent_dir / relative).resolve()
        if (
            relative != "gemini_handoff.pdf"
            or not _is_within(packet_path, self.agent_dir)
            or not packet_path.is_file()
            or _sha256(packet_path) != packet.get("sha256")
            or packet.get("request_id") != request.get("request_id")
            or packet.get("stage") != request.get("stage")
            or packet.get("revision") != route.get("revision")
        ):
            raise BridgeError("Gemini handoff packet is missing, changed, or stale")
        paths.append((packet_path, relative))
        return paths

    def _stage_evidence(
        self,
        request_id: str,
        handoff: dict[str, Any],
        *,
        handoff_revision: int,
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
            for frame in evidence_input.get("reference_frames") or []:
                sampled_metadata[Path(frame["path"]).resolve()] = {
                    "role": "reference_frame", "timestamp_seconds": frame["timestamp_seconds"],
                    "source_path": frame["path"],
                    "origin": frame.get("origin", "video_analyzer"),
                }
            sampled = evidence_input.get("sampled_frames") or {}
            for group in sampled.get("groups") or []:
                source = group.get("source") or {}
                window = group.get("request") or {}
                for frame in group.get("frames") or []:
                    absolute = frame.get("absolute_path")
                    if absolute:
                        sampled_metadata[Path(str(absolute)).resolve()] = {
                            "role": "sampled_frame",
                            "clip_id": source.get("clip_id"),
                            "group_id": group.get("group_id"),
                            "timestamp_seconds": frame.get("timestamp_seconds"),
                            "window_start_seconds": window.get("start_seconds"),
                            "window_end_seconds": window.get("end_seconds"),
                            "origin": group.get("sampling_method"),
                            "source_path": source.get("path"),
                        }
            evidence_files = list(evidence_input.get("files") or [])
            evidence_files.sort(
                key=lambda item: (
                    0
                    if Path(str(item)).resolve() in sampled_metadata
                    else 1,
                    str(item),
                )
            )
            for item in evidence_files:
                source = Path(str(item))
                try:
                    relative = source.resolve().relative_to(self.runner.project.root.resolve())
                except (OSError, ValueError):
                    continue
                metadata = sampled_metadata.get(source.resolve(), {"role": "stage_evidence"})
                candidates.append((str(Path("project") / relative), source, metadata))

        if (
            handoff.get("stage") == "footage"
            and self.runner.artifacts.exists("footage_profiles_scaffold")
        ):
            manifest_path = self._write_footage_evidence_manifest(
                request_id,
                handoff_revision,
                evidence_input if isinstance(evidence_input, dict) else {},
            )
            candidates.append(
                (
                    "inputs/footage_evidence_manifest.json",
                    manifest_path,
                    {
                        "role": "input_artifact",
                        "artifact": "footage_evidence_manifest",
                    },
                )
            )

        result: list[tuple[Path, str, dict[str, Any]]] = []
        seen: set[Path] = set()
        total_bytes = 0
        eligible: dict[Path, tuple[int, str]] = {}
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
            eligible[resolved] = (size, metadata.get("role", ""))
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
        if handoff.get("stage") == "analyze":
            exported = {item[2].get("artifact") for item in result}
            frames = [item[2] for item in result if item[2].get("role") == "reference_frame"]
            scaffold = self.runner.artifacts.read("reference_blueprint_scaffold")
            if "reference_blueprint_scaffold" not in exported or any(
                not any(segment["start_seconds"] <= frame["timestamp_seconds"] <= segment["end_seconds"]
                        for frame in frames)
                for segment in scaffold["segments"]
            ):
                frame_count = sum(role == "reference_frame" for _, role in eligible.values())
                raise BridgeError(
                    "analyze evidence is missing or exceeds bridge limits; "
                    f"prepared evidence has {len(eligible)} files including {frame_count} reference frames "
                    f"and {sum(size for size, _ in eligible.values())} bytes; "
                    f"configured max_evidence_files={self.config.max_evidence_files}, "
                    f"max_evidence_bytes={self.config.max_evidence_bytes}; "
                    "images covering every reference window are required before publishing"
                )
        if handoff.get("stage") == "footage":
            scaffold_has_clips = (
                self.runner.artifacts.exists("footage_profiles_scaffold")
                and bool(
                    self.runner.artifacts.read("footage_profiles_scaffold").get("clips")
                )
            )
            expected_paths = {
                Path(str(frame["absolute_path"])).resolve()
                for group in (evidence_input.get("sampled_frames") or {}).get("groups") or []
                for frame in group.get("frames") or []
            }
            expected_hashes = {_sha256(path) for path in expected_paths}
            prepared_frames = [
                descriptor
                for _, _, descriptor in result
                if descriptor.get("role") == "sampled_frame"
            ]
            prepared_hashes = {str(item.get("sha256")) for item in prepared_frames}
            exported_artifacts = {item[2].get("artifact") for item in result}
            if (
                (scaffold_has_clips and not expected_paths)
                or len(expected_paths) != len(prepared_frames)
                or expected_hashes != prepared_hashes
                or (
                    expected_paths
                    and "footage_profiles_scaffold" not in exported_artifacts
                )
                or (expected_paths and "footage_evidence_manifest" not in exported_artifacts)
            ):
                raise BridgeError(
                    "footage evidence is missing or exceeds bridge limits; "
                    f"prepared {len(prepared_frames)} of {len(expected_paths)} sampled frames; "
                    f"prepared artifacts={sorted(str(item) for item in exported_artifacts)}; "
                    f"configured max_evidence_files={self.config.max_evidence_files}, "
                    f"max_evidence_bytes={self.config.max_evidence_bytes}; all selected "
                    "frames for the current coarse/refinement pass are required before publishing"
                )
        return result

    def _write_footage_evidence_manifest(
        self,
        request_id: str,
        handoff_revision: int,
        evidence_input: dict[str, Any],
    ) -> Path:
        sampled = evidence_input.get("sampled_frames") or {}
        groups_by_clip: dict[str, list[dict[str, Any]]] = {}
        for group in sampled.get("groups") or []:
            source = group.get("source") or {}
            clip_id = str(source.get("clip_id") or "")
            window = group.get("request") or {}
            frames = []
            for frame in group.get("frames") or []:
                absolute = Path(str(frame.get("absolute_path") or "")).resolve()
                relative = absolute.relative_to(self.runner.project.root.resolve())
                frames.append(
                    {
                        "timestamp_seconds": frame.get("timestamp_seconds"),
                        "evidence_ref": (
                            Path("evidence")
                            / request_id
                            / "project"
                            / relative
                        ).as_posix(),
                        "sha256": _sha256(absolute),
                    }
                )
            groups_by_clip.setdefault(clip_id, []).append(
                {
                    "range_id": group.get("group_id"),
                    "sample_kind": group.get("sample_kind") or "legacy",
                    "handoff_revision": group.get("handoff_revision"),
                    "start_seconds": window.get("start_seconds"),
                    "end_seconds": window.get("end_seconds"),
                    "checkpoint_state": group.get("checkpoint_state") or "legacy_available",
                    "frames": frames,
                }
            )

        checkpoint_path = self.runner.project.cache_dir / "footage-preprocess.json"
        checkpoint: dict[str, Any] = {}
        if checkpoint_path.is_file():
            try:
                loaded = self._read_json(checkpoint_path)
                if isinstance(loaded, dict):
                    checkpoint = loaded
            except (OSError, UnicodeError, json.JSONDecodeError, BridgeError):
                checkpoint = {}
        cached_sources = {
            str(key): value
            for key, value in (checkpoint.get("clips") or {}).items()
            if isinstance(value, dict)
        }
        scaffold = self.runner.artifacts.read("footage_profiles_scaffold")
        clips = []
        batch_metadata = sampled.get("batch") or {}
        active_clip_ids = {
            str(item) for item in batch_metadata.get("clip_ids") or []
        }
        for clip in scaffold.get("clips") or []:
            if active_clip_ids and str(clip.get("clip_id") or "") not in active_clip_ids:
                continue
            raw_source = str(clip.get("path") or "")
            source = Path(raw_source)
            if not source.is_absolute():
                source = self.runner.project.root / source
            source = source.resolve()
            source_ref = source.relative_to(self.runner.project.root.resolve()).as_posix()
            cached = cached_sources.get(str(source)) or {}
            cached_source = cached.get("source") or {}
            source_sha256 = _sha256(source)
            preprocess_state = (
                "completed"
                if cached.get("status") == "completed"
                and cached_source.get("sha256") == source_sha256
                else "legacy_available"
            )
            ranges = sorted(
                groups_by_clip.get(str(clip.get("clip_id") or ""), []),
                key=lambda item: (
                    float(item.get("start_seconds") or 0.0),
                    str(item.get("range_id") or ""),
                ),
            )
            clips.append(
                {
                    "clip_id": clip.get("clip_id"),
                    "source_ref": source_ref,
                    "source_sha256": source_sha256,
                    "preprocessing_checkpoint_state": preprocess_state,
                    "duration_seconds": clip.get("duration_seconds"),
                    "fps": clip.get("fps"),
                    "candidate_ranges": ranges,
                }
            )
        range_count = sum(len(clip["candidate_ranges"]) for clip in clips)
        frame_count = sum(
            len(item["frames"])
            for clip in clips
            for item in clip["candidate_ranges"]
        )
        checkpoint_entries = list(cached_sources.values())
        completed_preprocessing = sum(
            entry.get("status") == "completed" for entry in checkpoint_entries
        )
        if checkpoint_entries and completed_preprocessing == len(checkpoint_entries):
            preprocessing_state = "completed"
        elif checkpoint_entries:
            preprocessing_state = "partial"
        else:
            preprocessing_state = "legacy_available"
        manifest = {
            "version": "1.0",
            "artifact": "footage_evidence_manifest",
            "job_id": self.config.project_id,
            "request_id": request_id,
            "stage": "footage",
            "revision": handoff_revision,
            "pipeline_revision": self.runner.state.revision,
            "batch": batch_metadata or None,
            "preprocessing": {
                "config_fingerprint": checkpoint.get("config_fingerprint"),
                "checkpoint_state": preprocessing_state,
                "completed_clip_count": completed_preprocessing,
                "tracked_clip_count": len(checkpoint_entries),
            },
            "completion": {
                "phase": sampled.get("selection") or "coarse",
                "checkpoint_state": "completed",
                "candidate_range_count": range_count,
                "completed_range_count": range_count,
                "frame_count": frame_count,
            },
            "clips": clips,
        }
        output = (
            self.runner.project.cache_dir
            / "footage-bundles"
            / f"{request_id}.json"
        )
        _atomic_json(output, manifest)
        return output

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
            "revision",
            "review",
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
        if "revision" in response:
            revision = response["revision"]
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise BridgeResponseError("response revision must be an integer")
            if revision != active.get("revision", self.runner.state.revision):
                raise BridgeResponseError("response revision does not match active request")
        elif (
            response.get("stage") == "footage"
            and int(active.get("revision", self.runner.state.revision)) > 0
        ):
            raise BridgeResponseError(
                "footage response revision is required after refinement"
            )
        if "review" in response and not isinstance(response["review"], dict):
            raise BridgeResponseError("response review must be a JSON object")
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
    def _response_schema(
        output_contract: dict[str, Any], *, stage: str | None = None,
        footage_batch: bool = False,
    ) -> dict[str, Any]:
        payload_schema = output_contract
        decisions = ["APPROVED", "REVISION_REQUIRED"]
        review_conditions: list[dict[str, Any]] = [{
            "if": {"properties": {"decision": {"const": "REVISION_REQUIRED"}}},
            "then": {
                "required": ["revision_targets"],
                "properties": {"revision_targets": {"minItems": 1}},
            },
        }]
        root_conditions: list[dict[str, Any]] = []
        if stage == "footage":
            payload_schema = {
                "type": "object",
                "artifact": (
                    "footage_semantic_batch"
                    if footage_batch
                    else output_contract.get("artifact")
                ),
                "rules": output_contract.get("rules") or [],
                "anyOf": [output_contract, FOOTAGE_REFINEMENT_REQUEST_SCHEMA],
                "refinement_schema": FOOTAGE_REFINEMENT_REQUEST_SCHEMA,
            }
            decisions = ["APPROVED", "REQUEST_REFINEMENT"]
            review_conditions = []
            root_conditions = [
                {
                    "if": {
                        "required": ["review"],
                        "properties": {
                            "review": {
                                "required": ["decision"],
                                "properties": {
                                    "decision": {"const": "REQUEST_REFINEMENT"}
                                },
                            }
                        },
                    },
                    "then": {
                        "properties": {
                            "payload": FOOTAGE_REFINEMENT_REQUEST_SCHEMA
                        }
                    },
                },
                {
                    "if": {
                        "required": ["review"],
                        "properties": {
                            "review": {
                                "required": ["decision"],
                                "properties": {
                                    "decision": {"const": "APPROVED"}
                                },
                            }
                        },
                    },
                    "then": {"properties": {"payload": output_contract}},
                },
            ]
        review_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["actor", "decision", "revision"],
            "properties": {
                "actor": {"const": "gpt"},
                "decision": {"enum": decisions},
                "revision": {"type": "integer", "minimum": 0},
                "revision_targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["segment_id", "reason"],
                        "properties": {
                            "segment_id": {"type": "string", "minLength": 1},
                            "reason": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "gemini_acknowledgement": {"type": "string", "minLength": 1},
            },
        }
        if review_conditions:
            review_schema["allOf"] = review_conditions
        schema = {
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
                "payload": payload_schema,
                "revision": {"type": "integer", "minimum": 0},
                "review": review_schema,
            },
        }
        if root_conditions:
            schema["allOf"] = root_conditions
        return schema

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
            "sampling",
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

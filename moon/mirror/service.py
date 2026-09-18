from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from moon.atomic import atomic_write_json
from moon.core.project import MoonProject
from moon.media.inspection import VIDEO_EXTENSIONS
from moon.media.probe import probe_media
from moon.mirror.models import PROJECT_PROTOCOL, AssetRecord, normalize_project_path, stable_asset_id
from moon.mirror.proxy import AUDIO_EXTENSIONS, ProxyBuilder
from moon.mirror.transport import MirrorTransport


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ProjectMirrorService:
    """Build a durable public mirror without leaking local absolute paths."""

    def __init__(
        self,
        project: MoonProject,
        transport: MirrorTransport,
        *,
        proxy_builder: ProxyBuilder | None = None,
    ) -> None:
        self.project = project
        self.transport = transport
        self.proxy_builder = proxy_builder or ProxyBuilder()
        self.local_dir = project.moon_dir / "project-mirror-v2"
        self.internal_manifest_path = self.local_dir / "manifest.json"
        self.proxy_cache_dir = self.local_dir / "proxies"

    def sync(self) -> dict[str, Any]:
        previous = self._load_internal_manifest()
        previous_assets = {item["asset_id"]: item for item in previous.get("assets", [])}
        assets: list[AssetRecord] = []
        changed: list[dict[str, Any]] = []

        for role, source in self._source_files():
            relative_path = source.relative_to(self.project.root).as_posix()
            asset_id = stable_asset_id(relative_path)
            stat = source.stat()
            prior = previous_assets.get(asset_id)
            content_sha256 = (
                prior["content_sha256"]
                if prior
                and int(prior.get("size_bytes", -1)) == stat.st_size
                and int(prior.get("modified_ns", -1)) == stat.st_mtime_ns
                else _sha256(source)
            )
            unchanged = bool(prior and prior.get("content_sha256") == content_sha256)
            media = self._media_metadata(source, prior if unchanged else None)
            proxy_path = self.proxy_builder.proxy_relative_path(relative_path, source, media)
            cached_proxy = (
                self.proxy_cache_dir
                / asset_id
                / f"{content_sha256[:16]}{Path(proxy_path).suffix.lower()}"
            )
            remote_proxy = self.transport.path(proxy_path)
            if unchanged and prior and Path(str(prior.get("proxy_cache", ""))).is_file():
                cached_proxy = Path(str(prior["proxy_cache"]))
            if not unchanged or not cached_proxy.exists():
                self.proxy_builder.build(source, cached_proxy, media)
            if prior and prior.get("proxy_path") != proxy_path:
                self.transport.remove(str(prior["proxy_path"]))
            if not unchanged or not remote_proxy.exists():
                self.transport.copy_file(cached_proxy, proxy_path)
            status = "ready" if unchanged else ("invalidated" if prior else "new")
            record = AssetRecord(
                asset_id=asset_id,
                role=role,
                relative_path=relative_path,
                content_sha256=content_sha256,
                size_bytes=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                proxy_path=proxy_path,
                duration_seconds=media.get("duration_seconds"),
                width=media.get("width"),
                height=media.get("height"),
                fps=media.get("fps"),
                has_video=bool(media.get("has_video")),
                has_audio=bool(media.get("has_audio")),
                status=status,
            )
            assets.append(record)
            if not unchanged:
                changed.append(
                    {
                        "asset_id": asset_id,
                        "reason": "content_changed" if prior else "asset_added",
                        "previous_sha256": prior.get("content_sha256") if prior else None,
                        "content_sha256": content_sha256,
                    }
                )

        current_ids = {asset.asset_id for asset in assets}
        deleted: list[dict[str, Any]] = []
        for asset_id, prior in sorted(previous_assets.items()):
            if asset_id in current_ids:
                continue
            self.transport.remove(str(prior["proxy_path"]))
            deleted.append(
                {
                    "asset_id": asset_id,
                    "relative_path": prior["relative_path"],
                    "previous_sha256": prior["content_sha256"],
                    "reason": "asset_deleted",
                }
            )

        generation = int(previous.get("generation", 0))
        if changed or deleted or not previous:
            generation += 1
        now = _utc_now()
        project_config = json.loads(self.project.project_path.read_text(encoding="utf-8"))
        public_assets = [asset.public_dict() for asset in sorted(assets, key=lambda item: item.relative_path)]
        public_manifest = {
            "protocol": PROJECT_PROTOCOL,
            "project_id": self.transport.root.name,
            "generation": generation,
            "updated_at": now,
            "project": {
                "reference": project_config.get("reference", "reference.mp4"),
                "footage_dir": project_config.get("footage_dir", "footage"),
                "output_dir": project_config.get("output_dir", "output"),
            },
            "assets": public_assets,
            "tombstones": deleted,
            "gpt_instructions_path": "GPT_INSTRUCTIONS.md",
        }
        internal_manifest = dict(public_manifest)
        internal_manifest["assets"] = []
        for asset in sorted(assets, key=lambda item: item.relative_path):
            item = asset.internal_dict()
            item["source_path"] = str((self.project.root / asset.relative_path).resolve())
            item["proxy_cache"] = str(
                (
                    self.proxy_cache_dir
                    / asset.asset_id
                    / f"{asset.content_sha256[:16]}{Path(asset.proxy_path).suffix.lower()}"
                ).resolve()
            )
            internal_manifest["assets"].append(item)

        invalidation = {
            "protocol": PROJECT_PROTOCOL,
            "project_id": self.transport.root.name,
            "generation": generation,
            "created_at": now,
            "changed": changed,
            "deleted": deleted,
            "requires_reanalysis": bool(changed or deleted),
        }
        self.local_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.internal_manifest_path, internal_manifest)
        public_project = {
            "protocol": PROJECT_PROTOCOL,
            "project_id": self.transport.root.name,
            "manifest": "project_manifest.json",
            "instructions": "GPT_INSTRUCTIONS.md",
            "active_task": "agent/current.json",
            "reference": project_config.get("reference", "reference.mp4"),
            "footage_dir": project_config.get("footage_dir", "footage"),
        }
        self.transport.write_json("project.json", public_project)
        self.transport.write_json("project_manifest.json", public_manifest)
        self.transport.write_json("manifest.json", public_manifest)
        self.transport.write_text("GPT_INSTRUCTIONS.md", self._gpt_instructions())
        self.transport.write_json("analysis/invalidation.json", invalidation)
        self._sync_persistent_artifacts(previous)
        return public_manifest

    def resolve_asset(self, asset_id: str, *, content_sha256: str | None = None) -> Path:
        manifest = self._load_internal_manifest()
        item = next((value for value in manifest.get("assets", []) if value.get("asset_id") == asset_id), None)
        if item is None:
            raise KeyError(f"unknown asset_id: {asset_id}")
        if content_sha256 and item.get("content_sha256") != content_sha256:
            raise ValueError(f"stale asset identity: {asset_id}")
        source = Path(str(item["source_path"])).resolve()
        try:
            source.relative_to(self.project.root)
        except ValueError as exc:
            raise ValueError(f"asset escaped project root: {asset_id}") from exc
        if not source.is_file() or _sha256(source) != item.get("content_sha256"):
            raise ValueError(f"asset changed since mirror generation: {asset_id}")
        return source

    def _load_internal_manifest(self) -> dict[str, Any]:
        if not self.internal_manifest_path.exists():
            return {}
        payload = json.loads(self.internal_manifest_path.read_text(encoding="utf-8"))
        if payload.get("protocol") != PROJECT_PROTOCOL:
            return {}
        return payload

    def _source_files(self) -> Iterable[tuple[str, Path]]:
        config = json.loads(self.project.project_path.read_text(encoding="utf-8"))
        seen: set[Path] = set()
        reference = self.project.root / str(config.get("reference", "reference.mp4"))
        if reference.is_file():
            resolved = reference.resolve()
            seen.add(resolved)
            yield "reference", resolved
        footage_dir = self.project.root / str(config.get("footage_dir", "footage"))
        if footage_dir.is_dir():
            for path in sorted(footage_dir.rglob("*")):
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
                    resolved = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield "footage", resolved
        for path in sorted(self.project.root.iterdir()):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                resolved = path.resolve()
                if resolved not in seen:
                    yield "audio", resolved

    @staticmethod
    def _media_metadata(source: Path, prior: dict[str, Any] | None) -> dict[str, Any]:
        if prior:
            return {
                key: prior.get(key)
                for key in ("duration_seconds", "width", "height", "fps", "has_video", "has_audio")
            }
        if source.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
            return probe_media(source)
        return {}

    def _sync_persistent_artifacts(self, previous: dict[str, Any]) -> None:
        prior_hashes = previous.get("persistent_artifacts", {})
        current: dict[str, str] = {}
        analysis_entries: list[dict[str, Any]] = []
        for local_root, remote_root in (
            (self.project.artifacts_dir, "artifacts"),
            (self.project.evidence_dir, "analysis/evidence"),
        ):
            if not local_root.exists():
                continue
            for path in sorted(local_root.rglob("*")):
                if not path.is_file():
                    continue
                relative = normalize_project_path(path.relative_to(local_root).as_posix())
                remote = f"{remote_root}/{relative}"
                digest = _sha256(path)
                current[remote] = digest
                if prior_hashes.get(remote) != digest or self.transport.read_bytes(remote) is None:
                    self.transport.copy_file(path, remote)
                if remote_root.startswith("analysis/"):
                    analysis_entries.append(
                        {
                            "path": remote,
                            "sha256": digest,
                            "size_bytes": path.stat().st_size,
                        }
                    )
        for remote in sorted(set(prior_hashes) - set(current)):
            self.transport.remove(remote)
        self.transport.write_json(
            "analysis/index.json",
            {
                "protocol": PROJECT_PROTOCOL,
                "project_id": self.transport.root.name,
                "updated_at": _utc_now(),
                "files": analysis_entries,
            },
        )
        payload = self._load_internal_manifest()
        payload["persistent_artifacts"] = current
        atomic_write_json(self.internal_manifest_path, payload)

    @staticmethod
    def _gpt_instructions() -> str:
        return """# MON EDIT Project Mirror V2

1. Read `agent/current.json` and then its `request_path`.
2. Read `project_manifest.json` before inspecting media or evidence.
3. Treat `asset_id` plus `content_sha256` as immutable source identity.
4. Media at each asset's `mirror_path` is a timeline-aligned viewing proxy.
5. Return only semantic results. Never choose pipeline state, owner, or next action.
6. Reference sources by `asset_id`, `relative_path`, and source timestamps.
7. Never invent local machine paths, asset IDs, filenames, or timestamps.
8. Write the response only to the active task's `response_path`.
9. If `validation.json` reports an error, replace the unaccepted response with a corrected one.
10. A task with `receipt.json` is immutable and must not be edited.
"""

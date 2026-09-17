from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.mirror.models import AssetRecord, normalize_project_path, stable_asset_id
from moon.mirror.proxy import ProxyBuilder
from moon.mirror.service import ProjectMirrorService
from moon.mirror.transport import MirrorTransport


class CopyProxyBuilder:
    @staticmethod
    def proxy_relative_path(asset_id: str, source: Path, media: dict) -> str:
        return f"proxies/{asset_id}{source.suffix}"

    @staticmethod
    def build(source: Path, destination: Path, media: dict) -> dict:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return media


def test_asset_id_is_stable_across_case_and_separator() -> None:
    assert stable_asset_id("Footage\\Clip.MP4") == stable_asset_id("footage/clip.mp4")


@pytest.mark.parametrize("value", ["../clip.mp4", "/clip.mp4", "C:/clip.mp4", "footage/../clip.mp4"])
def test_normalized_path_rejects_escape(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_project_path(value)


def test_public_asset_omits_local_mtime() -> None:
    record = AssetRecord("asset_1", "footage", "footage/a.mp4", "a" * 64, 4, 123, "proxies/a.mp4")
    assert "modified_ns" not in record.public_dict()
    assert record.internal_dict()["modified_ns"] == 123


def test_proxy_policy_converts_unsupported_video_container() -> None:
    assert ProxyBuilder.proxy_relative_path("asset_1", Path("clip.webm"), {"has_video": True}) == "proxies/asset_1.mp4"
    assert ProxyBuilder.proxy_relative_path("asset_1", Path("clip.mov"), {"has_video": True}) == "proxies/asset_1.mov"


def test_transport_rejects_traversal(tmp_path: Path) -> None:
    transport = MirrorTransport(tmp_path, "project")
    with pytest.raises(ValueError):
        transport.path("tasks/../../secret.json")


def test_incremental_sync_invalidates_changed_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    footage = project.root / "footage"
    footage.mkdir()
    source = footage / "clip.wav"
    source.write_bytes(b"first")
    monkeypatch.setattr(
        "moon.mirror.service.probe_media",
        lambda path: {"duration_seconds": 1.0, "width": None, "height": None, "fps": None, "has_video": False, "has_audio": True},
    )
    service = ProjectMirrorService(
        project,
        MirrorTransport(tmp_path / "drive", "project"),
        proxy_builder=CopyProxyBuilder(),
    )
    first = service.sync()
    source.write_bytes(b"second")
    second = service.sync()
    invalidation = json.loads((tmp_path / "drive" / "MON_EDIT" / "projects" / "project" / "analysis" / "invalidation.json").read_text())

    assert second["generation"] == first["generation"] + 1
    assert invalidation["changed"][0]["reason"] == "content_changed"
    assert str(project.root) not in json.dumps(second)

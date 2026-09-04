from __future__ import annotations

import json
import re
import tempfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SOURCE = REPOSITORY_ROOT / "packaging" / "mcpb"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "dist" / "moon-local.mcpb"
ROOT_FILES = ("manifest.json", "launcher.py", "pyproject.toml", "README.md")
RUNTIME_PACKAGE_DIRS = ("moon", "tools", "lib")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


def load_and_validate_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or PACKAGE_SOURCE / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {"manifest_version", "name", "version", "description", "author", "server"}
    missing = required.difference(data)
    if missing:
        raise ValueError(f"manifest is missing required fields: {sorted(missing)}")
    if data["manifest_version"] != "0.4":
        raise ValueError("Moon Local currently targets MCPB manifest version 0.4")
    if not isinstance(data["name"], str) or not data["name"]:
        raise ValueError("manifest name must be a non-empty string")
    if not isinstance(data["version"], str) or SEMVER.fullmatch(data["version"]) is None:
        raise ValueError("manifest version must use semantic versioning")
    if not isinstance(data["author"], dict) or not data["author"].get("name"):
        raise ValueError("manifest author.name is required")

    server = data["server"]
    if not isinstance(server, dict) or server.get("type") != "uv":
        raise ValueError("manifest server.type must be 'uv'")
    entry_point = server.get("entry_point")
    if entry_point not in ROOT_FILES or not (PACKAGE_SOURCE / entry_point).is_file():
        raise ValueError("manifest server.entry_point must name a packaged root file")
    mcp_config = server.get("mcp_config")
    if not isinstance(mcp_config, dict) or mcp_config.get("command") != "uv":
        raise ValueError("manifest server.mcp_config.command must be 'uv'")
    expected_args = ["run", entry_point, "--project", "${user_config.project_root}"]
    if mcp_config.get("args") != expected_args:
        raise ValueError("manifest must launch its relative entry point with the configured project root")
    if "${__dirname}" in json.dumps(mcp_config):
        raise ValueError("UV launch configuration must not pass a virtualized extension path")
    if not (PACKAGE_SOURCE / "pyproject.toml").is_file():
        raise ValueError("UV extensions require a root-level pyproject.toml")
    project_config = data.get("user_config", {}).get("project_root", {})
    if project_config.get("type") != "directory" or not project_config.get("required"):
        raise ValueError("manifest project_root must be a required directory setting")
    return data


def bundle_files() -> Iterable[tuple[Path, str]]:
    for name in ROOT_FILES:
        yield PACKAGE_SOURCE / name, name
    yield REPOSITORY_ROOT / "LICENSE", "LICENSE"
    for package_dir in RUNTIME_PACKAGE_DIRS:
        for path in sorted((REPOSITORY_ROOT / package_dir).rglob("*.py")):
            yield path, path.relative_to(REPOSITORY_ROOT).as_posix()


def _write_archive_file(archive: zipfile.ZipFile, source: Path, archive_name: str) -> None:
    """Write one portable regular-file entry with canonical POSIX archive paths."""
    info = zipfile.ZipInfo(archive_name)
    info.create_system = 0
    info.external_attr = 0x20  # DOS archive bit; this is a regular file, not a directory.
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, source.read_bytes())


def verify_archive(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if names.count("manifest.json") != 1:
            raise ValueError("MCPB must contain exactly one root-level manifest.json")
        manifest = json.loads(archive.read("manifest.json"))
        entry_point = manifest["server"]["entry_point"]
        entry_path = PurePosixPath(entry_point)
        if entry_path.is_absolute() or ".." in entry_path.parts or entry_path.as_posix() != entry_point:
            raise ValueError(f"manifest entry point is not a canonical archive path: {entry_point!r}")
        if entry_point not in names:
            raise ValueError(f"manifest entry point is absent from MCPB archive: {entry_point}")
        launch_args = manifest["server"]["mcp_config"]["args"]
        if launch_args[:2] != ["run", entry_point]:
            raise ValueError("UV launch argument does not exactly match server.entry_point")
        if "${__dirname}" in json.dumps(manifest["server"]["mcp_config"]):
            raise ValueError("MCPB must not expose a virtualized extension path to UV")
        if "pyproject.toml" not in names:
            raise ValueError("UV MCPB is missing root-level pyproject.toml")
        required_runtime_files = {
            "moon/__init__.py",
            "moon/__main__.py",
            "moon/evidence.py",
            "moon/mcp_adapter.py",
            "tools/__init__.py",
            "tools/base_tool.py",
            "tools/analysis/footage_profile_builder.py",
            "tools/analysis/reference_candidate_ranker.py",
            "tools/analysis/reference_match_validator.py",
            "tools/video/reference_timeline_builder.py",
            "tools/video/reference_video_renderer.py",
            "lib/__init__.py",
        }
        missing_runtime_files = required_runtime_files.difference(names)
        if missing_runtime_files:
            raise ValueError(f"MCPB is missing bundled runtime files: {sorted(missing_runtime_files)}")
        return manifest


def build_mcpb(output: Path | None = None) -> Path:
    load_and_validate_manifest()
    destination = (output or DEFAULT_OUTPUT).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix="moon-local-",
        suffix=".mcpb.tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, archive_name in bundle_files():
                if not source.is_file():
                    raise FileNotFoundError(f"required MCPB input is missing: {source}")
                _write_archive_file(archive, source, archive_name)
        verify_archive(temporary_path)
        temporary_path.replace(destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination


def main() -> int:
    output = build_mcpb()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

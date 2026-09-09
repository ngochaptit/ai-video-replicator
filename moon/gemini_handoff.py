from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


PACKET_VERSION = "1.0"
PAGE_SIZE = (1240, 1754)
PAGE_DPI = 150
PAGE_MARGIN = 64


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        ("C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf")
        if mono
        else (
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _pdf_literal(value: str) -> bytes:
    safe = value.encode("ascii", "backslashreplace").decode("ascii")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii")


class GeminiHandoffPacketBuilder:
    """Build one visual, request-scoped packet from canonical Moon inputs."""

    def __init__(self, agent_dir: Path) -> None:
        self.agent_dir = agent_dir.resolve()
        self.title_font = _font(30, bold=True)
        self.heading_font = _font(22, bold=True)
        self.body_font = _font(18, mono=True)
        self.label_font = _font(20, bold=True)

    def build(
        self,
        request: dict[str, Any],
        route: dict[str, Any],
        output_path: Path,
    ) -> dict[str, Any]:
        stage = str(request.get("stage") or "")
        if stage not in {"analyze", "footage"}:
            raise ValueError("Gemini visual handoff packets require analyze or footage")
        artifacts, frames = self._load_inputs(request)
        scaffold_name = (
            "reference_blueprint_scaffold"
            if stage == "analyze"
            else "footage_profiles_scaffold"
        )
        scaffold = artifacts.get(scaffold_name) or {}
        frame_manifest = self._frame_manifest(frames, scaffold, stage=stage)
        source_manifest = [
            {
                "artifact": name,
                "path": value["path"],
                "sha256": value["sha256"],
            }
            for name, value in sorted(artifacts.items())
        ]
        manifest_core = {
            "version": PACKET_VERSION,
            "job_id": request["job_id"],
            "request_id": request["request_id"],
            "stage": request["stage"],
            "revision": route["revision"],
            "request_created_at": request["created_at"],
            "canonical_sources": source_manifest,
            "frames": frame_manifest,
            "response_schema_sha256": _sha256_bytes(
                _canonical_json(request["expected_response_schema"])
            ),
        }
        manifest_hash = _sha256_bytes(_canonical_json(manifest_core))
        manifest = {**manifest_core, "manifest_sha256": manifest_hash}

        pages: list[tuple[bytes, int, int]] = []
        pages.extend(self._route_pages(request, route, manifest_hash))
        if stage == "analyze":
            pages.extend(
                self._json_pages(
                    "Proposal packet",
                    artifacts.get("proposal_packet", {}).get("content"),
                )
            )
            pages.extend(
                self._json_pages(
                    "Video analysis brief",
                    artifacts.get("video_analysis_brief", {}).get("content"),
                )
            )
            pages.extend(self._scaffold_pages(scaffold.get("content") or {}))
        else:
            pages.extend(
                self._json_pages(
                    "Footage profiles scaffold",
                    scaffold.get("content"),
                )
            )
            pages.extend(
                self._json_pages(
                    "Sampling coverage summary",
                    artifacts.get("footage_evidence_catalog", {}).get("content"),
                )
            )
            for name, value in sorted(artifacts.items()):
                if name.startswith("footage_brief:"):
                    pages.extend(
                        self._json_pages(
                            f"Footage / video analysis brief: {value['path']}",
                            value["content"],
                        )
                    )
            pages.extend(
                self._text_pages(
                    "Coarse-to-fine review instructions",
                    "Review every coarse sampled frame before making semantic decisions. "
                    "Use only measured timestamps and in-range frame evidence. If an action "
                    "or interaction boundary remains ambiguous, return the strict "
                    "footage_refinement_request described in the response schema. Do not "
                    "execute Moon or local commands. Moon will sample the requested narrower "
                    "windows and publish a fresh request revision and packet. On a recheck, "
                    "inspect the requested windows and their added frames before returning a "
                    "complete footage_semantic_enrichment.",
                )
            )
        for frame, labels in zip(frames, frame_manifest, strict=True):
            pages.append(self._frame_page(frame["absolute_path"], labels))
        pages.extend(
            self._json_pages(
                "Response rules and semantic_enrichment schema",
                request["expected_response_schema"],
            )
        )

        created_at = self._created_at(request["created_at"])
        pdf = self._write_pdf(
            pages,
            manifest=_canonical_json(manifest),
            title=f"Moon Gemini handoff {request['job_id']}",
            subject=(
                f"job_id={request['job_id']} request_id={request['request_id']} "
                f"stage={stage} revision={route['revision']} manifest_sha256={manifest_hash}"
            ),
            created_at=created_at,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_bytes(pdf)
        temporary.replace(output_path)
        return {
            "version": PACKET_VERSION,
            "path": output_path.name,
            "job_id": request["job_id"],
            "request_id": request["request_id"],
            "stage": stage,
            "revision": route["revision"],
            "manifest_sha256": manifest_hash,
            "sha256": _sha256_bytes(pdf),
            "bytes": len(pdf),
            "frame_count": len(frames),
            "page_count": len(pages),
        }

    def _load_inputs(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        artifacts: dict[str, dict[str, Any]] = {}
        frames: list[dict[str, Any]] = []
        for descriptor in request.get("evidence") or []:
            relative = str(descriptor.get("path") or "")
            path = (self.agent_dir / relative).resolve()
            try:
                path.relative_to(self.agent_dir)
            except ValueError as exc:
                raise ValueError(f"unsafe packet input path: {relative!r}") from exc
            if not path.is_file() or _sha256_file(path) != descriptor.get("sha256"):
                raise ValueError(f"packet input is missing or changed: {relative!r}")
            artifact = descriptor.get("artifact")
            if artifact in {
                "proposal_packet",
                "video_analysis_brief",
                "reference_blueprint_scaffold",
                "footage_profiles_scaffold",
                "footage_evidence_catalog",
            }:
                content = json.loads(path.read_text(encoding="utf-8"))
                artifacts[str(artifact)] = {
                    "path": relative,
                    "sha256": descriptor["sha256"],
                    "content": content,
                }
            if (
                request.get("stage") == "footage"
                and descriptor.get("role") == "stage_evidence"
                and path.suffix.lower() == ".json"
            ):
                artifacts[f"footage_brief:{relative}"] = {
                    "path": relative,
                    "sha256": descriptor["sha256"],
                    "content": json.loads(path.read_text(encoding="utf-8")),
                }
            expected_role = (
                "reference_frame"
                if request.get("stage") == "analyze"
                else "sampled_frame"
            )
            if descriptor.get("role") == expected_role:
                frames.append({**descriptor, "absolute_path": path})
        frames.sort(
            key=lambda item: (
                str(item.get("clip_id") or ""),
                float(item.get("timestamp_seconds", 0.0)),
                str(item.get("group_id") or ""),
                str(item.get("path") or ""),
            )
        )
        return artifacts, frames

    @staticmethod
    def _frame_manifest(
        frames: list[dict[str, Any]], scaffold: dict[str, Any], *, stage: str
    ) -> list[dict[str, Any]]:
        if stage == "footage":
            return [
                {
                    "path": frame["path"],
                    "sha256": frame["sha256"],
                    "clip_id": frame.get("clip_id"),
                    "timestamp_seconds": float(frame["timestamp_seconds"]),
                    "sample_group": frame.get("group_id"),
                    "window": {
                        "start_seconds": frame.get("window_start_seconds"),
                        "end_seconds": frame.get("window_end_seconds"),
                    },
                    "origin": frame.get("origin"),
                }
                for frame in frames
            ]
        segments = (scaffold.get("content") or {}).get("segments") or []
        result = []
        for frame in frames:
            timestamp = float(frame["timestamp_seconds"])
            windows = [
                {
                    "segment_id": str(segment["id"]),
                    "start_seconds": segment["start_seconds"],
                    "end_seconds": segment["end_seconds"],
                }
                for segment in segments
                if isinstance(segment, dict)
                and segment.get("id") is not None
                and isinstance(segment.get("start_seconds"), (int, float))
                and isinstance(segment.get("end_seconds"), (int, float))
                and float(segment["start_seconds"]) <= timestamp <= float(segment["end_seconds"])
            ]
            result.append(
                {
                    "path": frame["path"],
                    "sha256": frame["sha256"],
                    "timestamp_seconds": timestamp,
                    "origin": frame.get("origin"),
                    "associated_segments": windows,
                }
            )
        return result

    def _route_pages(
        self, request: dict[str, Any], route: dict[str, Any], manifest_hash: str
    ) -> list[tuple[bytes, int, int]]:
        text = "\n".join(
            [
                f"job_id: {request['job_id']}",
                f"request_id: {request['request_id']}",
                f"stage: {request['stage']}",
                f"revision: {route['revision']}",
                f"current_actor: {route['current_actor']}",
                f"next_actor: {route['next_actor']}",
                f"next_action: {route['next_action']}",
                f"packet_manifest_sha256: {manifest_hash}",
                "",
                "TASK",
                str(route["task"]),
                "",
                "EXPECTED OUTPUT",
                json.dumps(route["expected_output"], ensure_ascii=True, sort_keys=True, indent=2),
                "",
                "EXACT TERMINAL ACKNOWLEDGEMENT",
                str(route["completion_contract"]["terminal_acknowledgement"]),
                "",
                "ROUTING / COMPLETION CONTRACT",
                json.dumps(route["completion_contract"], ensure_ascii=True, sort_keys=True, indent=2),
            ]
        )
        return self._text_pages("Moon / Gemini visual handoff", text)

    def _scaffold_pages(self, scaffold: dict[str, Any]) -> list[tuple[bytes, int, int]]:
        measured = {
            "source": scaffold.get("source"),
            "analysis_meta": scaffold.get("analysis_meta"),
            "segments": [
                {
                    key: segment.get(key)
                    for key in ("id", "start_seconds", "end_seconds", "duration_seconds", "boundary_basis", "evidence")
                    if key in segment
                }
                for segment in scaffold.get("segments") or []
                if isinstance(segment, dict)
            ],
        }
        return self._json_pages(
            "Reference blueprint scaffold: measured segments and timing", measured
        )

    def _json_pages(self, title: str, value: Any) -> list[tuple[bytes, int, int]]:
        text = json.dumps(
            value if value is not None else {},
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        return self._text_pages(title, text)

    def _text_pages(self, title: str, text: str) -> list[tuple[bytes, int, int]]:
        width, height = PAGE_SIZE
        line_height = 25
        lines = self._wrapped_lines(text, width - 2 * PAGE_MARGIN)
        pages: list[tuple[bytes, int, int]] = []
        page = Image.new("RGB", PAGE_SIZE, "white")
        draw = ImageDraw.Draw(page)
        draw.text((PAGE_MARGIN, PAGE_MARGIN), title, font=self.title_font, fill="#111111")
        y = PAGE_MARGIN + 52
        for line in lines:
            if y + line_height > height - PAGE_MARGIN:
                pages.append(self._encode_page(page))
                page = Image.new("RGB", PAGE_SIZE, "white")
                draw = ImageDraw.Draw(page)
                draw.text(
                    (PAGE_MARGIN, PAGE_MARGIN),
                    f"{title} (continued)",
                    font=self.heading_font,
                    fill="#111111",
                )
                y = PAGE_MARGIN + 44
            draw.text((PAGE_MARGIN, y), line, font=self.body_font, fill="#202020")
            y += line_height
        pages.append(self._encode_page(page))
        return pages

    def _wrapped_lines(self, text: str, max_width: int) -> list[str]:
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        result: list[str] = []
        for original in text.splitlines() or [""]:
            line = original.expandtabs(2)
            if not line:
                result.append("")
                continue
            while probe.textlength(line, font=self.body_font) > max_width:
                low, high = 1, len(line)
                while low < high:
                    middle = (low + high + 1) // 2
                    if probe.textlength(line[:middle], font=self.body_font) <= max_width:
                        low = middle
                    else:
                        high = middle - 1
                split = max(1, low)
                result.append(line[:split])
                line = line[split:]
            result.append(line)
        return result

    def _frame_page(
        self, path: Path, labels: dict[str, Any]
    ) -> tuple[bytes, int, int]:
        page = Image.new("RGB", PAGE_SIZE, "white")
        draw = ImageDraw.Draw(page)
        if labels.get("clip_id") is not None:
            window = labels.get("window") or {}
            label_lines = [
                f"clip_id: {labels.get('clip_id')}",
                f"timestamp_seconds: {labels['timestamp_seconds']}",
                f"sample group: {labels.get('sample_group')}",
                "sample window: "
                f"[{window.get('start_seconds')}, {window.get('end_seconds')}]",
                f"origin: {labels.get('origin')}",
                f"measured evidence path/reference: {labels['path']}",
                f"sha256: {labels['sha256']}",
            ]
            frame_title = "Measured footage frame"
        else:
            segments = labels.get("associated_segments") or []
            segment_label = ", ".join(
                f"{item['segment_id']} [{item['start_seconds']}, {item['end_seconds']}]"
                for item in segments
            ) or "none"
            label_lines = [
                f"timestamp_seconds: {labels['timestamp_seconds']}",
                f"origin: {labels.get('origin')}",
                f"associated segment/window: {segment_label}",
                f"source evidence: {labels['path']}",
                f"sha256: {labels['sha256']}",
            ]
            frame_title = "Measured reference frame"
        y = PAGE_MARGIN
        draw.text(
            (PAGE_MARGIN, y),
            frame_title,
            font=self.title_font,
            fill="#111111",
        )
        y += 42
        for line in self._wrapped_lines(
            "\n".join(label_lines), PAGE_SIZE[0] - 2 * PAGE_MARGIN
        ):
            draw.text((PAGE_MARGIN, y), line, font=self.body_font, fill="#111111")
            y += 27
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail(
                (PAGE_SIZE[0] - 2 * PAGE_MARGIN, PAGE_SIZE[1] - y - PAGE_MARGIN),
                Image.Resampling.LANCZOS,
            )
            x = (PAGE_SIZE[0] - image.width) // 2
            image_y = y + max(0, (PAGE_SIZE[1] - PAGE_MARGIN - y - image.height) // 2)
            page.paste(image, (x, image_y))
        return self._encode_page(page)

    @staticmethod
    def _encode_page(page: Image.Image) -> tuple[bytes, int, int]:
        buffer = io.BytesIO()
        page.save(
            buffer,
            format="JPEG",
            quality=94,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        return buffer.getvalue(), page.width, page.height

    @staticmethod
    def _created_at(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("request created_at must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _write_pdf(
        pages: list[tuple[bytes, int, int]],
        *,
        manifest: bytes,
        title: str,
        subject: str,
        created_at: datetime,
    ) -> bytes:
        objects: list[bytes] = [b""]

        def reserve() -> int:
            objects.append(b"")
            return len(objects) - 1

        catalog_id = reserve()
        pages_id = reserve()
        manifest_id = reserve()
        filespec_id = reserve()
        page_ids: list[int] = []
        for jpeg, width, height in pages:
            image_id = reserve()
            content_id = reserve()
            page_id = reserve()
            page_ids.append(page_id)
            objects[image_id] = (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(jpeg)} >>\nstream\n".encode("ascii")
                + jpeg
                + b"\nendstream"
            )
            page_width = width * 72 / PAGE_DPI
            page_height = height * 72 / PAGE_DPI
            content = (
                f"q {page_width:.4f} 0 0 {page_height:.4f} 0 0 cm /Im0 Do Q\n"
            ).encode("ascii")
            objects[content_id] = (
                f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                + content
                + b"endstream"
            )
            objects[page_id] = (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
                f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        info_id = reserve()
        objects[catalog_id] = (
            f"<< /Type /Catalog /Pages {pages_id} 0 R /Names << /EmbeddedFiles "
            f"<< /Names [(packet_manifest.json) {filespec_id} 0 R] >> >> >>"
        ).encode("ascii")
        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        objects[pages_id] = (
            f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>"
        ).encode("ascii")
        objects[manifest_id] = (
            f"<< /Type /EmbeddedFile /Subtype /application#2Fjson /Length {len(manifest)} >>\nstream\n".encode("ascii")
            + manifest
            + b"\nendstream"
        )
        objects[filespec_id] = (
            f"<< /Type /Filespec /F (packet_manifest.json) /UF (packet_manifest.json) "
            f"/EF << /F {manifest_id} 0 R /UF {manifest_id} 0 R >> >>"
        ).encode("ascii")
        pdf_date = created_at.strftime("D:%Y%m%d%H%M%SZ")
        objects[info_id] = (
            b"<< /Title (" + _pdf_literal(title) + b") /Subject ("
            + _pdf_literal(subject)
            + b") /Creator (Moon deterministic Gemini handoff) "
            + b"/Producer (Moon Pillow packet builder) /CreationDate ("
            + pdf_date.encode("ascii")
            + b") /ModDate ("
            + pdf_date.encode("ascii")
            + b") >>"
        )

        output = io.BytesIO()
        output.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for object_id, value in enumerate(objects[1:], start=1):
            offsets.append(output.tell())
            output.write(f"{object_id} 0 obj\n".encode("ascii"))
            output.write(value)
            output.write(b"\nendobj\n")
        xref = output.tell()
        output.write(f"xref\n0 {len(objects)}\n".encode("ascii"))
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.write(
            (
                f"trailer\n<< /Size {len(objects)} /Root {catalog_id} 0 R "
                f"/Info {info_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n"
            ).encode("ascii")
        )
        return output.getvalue()

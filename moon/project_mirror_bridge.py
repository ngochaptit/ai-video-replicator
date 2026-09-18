from __future__ import annotations

import time
import re
from pathlib import Path
from typing import Any, Callable

from moon.agent_bridge import AgentBridgeService
from moon.drive_bridge import BridgeError, DriveBridgeConfig, MoonDriveBridge
from moon.footage_batches import FootageBatchPolicy, FootageSemanticProgress
from moon.footage_refinement import FootageRefinementService
from moon.handoff import AgentHandoffService
from moon.mirror.service import ProjectMirrorService
from moon.mirror.transport import MirrorTransport
from moon.project_mirror_migration import migrate_project_mirror_v2
from moon.runner.pipeline import PipelineRunner
from moon.tasks.exchange import TaskExchange
from moon.tasks.store import TaskStore
from moon.semantic_contracts import validate_semantic_submission


def bridge_for(runner: PipelineRunner, config: DriveBridgeConfig) -> MoonDriveBridge | ProjectMirrorBridge:
    if config.exchange_protocol == "project_mirror_v2":
        return ProjectMirrorBridge(runner, config)
    return MoonDriveBridge(runner, config)


class ProjectMirrorBridge:
    """Adapter that keeps the operator API while using immutable V2 task folders."""

    def __init__(
        self,
        runner: PipelineRunner,
        config: DriveBridgeConfig,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        config.validate(runner.project.root)
        if config.transport != "local_sync" or config.sync_root is None:
            raise BridgeError("project_mirror_v2 requires local_sync transport")
        self.runner = runner
        self.config = config
        self.migration = migrate_project_mirror_v2(runner.project)
        self.transport = MirrorTransport(config.sync_root, config.project_id)
        self.mirror = ProjectMirrorService(runner.project, self.transport)
        self.exchange = TaskExchange(TaskStore(runner.project), self.transport)
        self._sleep = sleeper

    def publish(self, stage: str) -> dict[str, Any]:
        manifest = self.mirror.sync()
        handoff = AgentHandoffService(self.runner).package(stage)
        revision = self.runner.state.revision
        inputs: dict[str, Any] = {
            "task": self._public_task(handoff.get("task") or {}),
            "asset_scope": self._asset_scope(manifest, stage),
            "analysis_index_path": "analysis/index.json",
        }
        progress = self._footage_progress() if stage == "footage" else None
        batch = progress.activate() if progress else None
        if progress and batch is None and progress.remaining_count() > 0:
            raise BridgeError("footage semantic batching has unfinished work but no eligible V2 batch")
        if batch:
            public_batch = progress.public_batch(batch)
            inputs["work_scope"] = {
                "clip_ids": public_batch.get("clip_ids") or [],
                "ranges": public_batch.get("ranges") or [],
                "frame_count": public_batch.get("frame_count"),
                "evidence_bytes": public_batch.get("evidence_bytes"),
            }
            revision = int(batch.get("revision", revision))
        preexisting_ids = set(self.exchange.store.index().get("tasks", {}))
        task = self.exchange.create(
            stage=stage,
            revision=revision,
            manifest=manifest,
            result_schema=self._result_schema(
                handoff["output_contract"], footage_batch=False
            ),
            inputs=inputs,
        )
        if batch:
            progress.bind_request(
                str(batch["batch_id"]),
                str(task["task_id"]),
                revision,
                [item["content_sha256"] for item in manifest.get("assets", [])],
            )
        request = {
            **task,
            "request_id": task["task_id"],
            "route": {"revision": task["revision"], "scope": inputs.get("work_scope")},
        }
        return {
            "status": "WAITING_AGENT",
            "idempotent": task["task_id"] in preexisting_ids,
            "request": request,
            "remote": {"transport": "local_sync", "remote_path": str(self.transport.root), "request_path": f"agent/tasks/{task['task_id']}/request.json"},
        }

    def poll_once(self) -> dict[str, Any] | None:
        current = self.transport.read_json("agent/current.json")
        if not current:
            return None
        task = self.exchange.store.read(str(current["task_id"]))
        if not task:
            return None
        manifest = self.transport.read_json("project_manifest.json") or {}
        result = self.exchange.poll(
            task,
            manifest,
            semantic_validator=lambda response: self._validate_semantic_response(
                task, response
            ),
        )
        if result["status"] == "waiting":
            return None
        if result["status"] == "correction_required":
            return {"status": "WAITING_GPT_CORRECTION", "request_id": task["task_id"], "error": result["error"]}
        response = result.get("response") or self.exchange.store.read(task["task_id"], "response.json")
        if response is None:
            return None
        if response.get("status") == "needs_refinement":
            refinement_result = response.get("result") or {}
            requests = refinement_result.get("requests") or []
            refinement = self._create_refinement(
                task,
                manifest,
                requests,
                partial_result=refinement_result.get("partial_result") or {},
            )
            return {"status": "WAITING_AGENT", "request_id": refinement["task_id"], "parent_task_id": task["task_id"]}

        def consume(accepted: dict[str, Any]) -> dict[str, Any]:
            payload = self._effective_payload(task, accepted["result"])
            if self.runner.state.next_stage() != task["stage"]:
                return {
                    "submission": {"accepted": False, "duplicate": True},
                    "resume": {"status": "already_advanced", "pipeline": self.runner.status()},
                }
            progress = self._footage_progress() if task["stage"] == "footage" else None
            batch_request_id = str(
                (task.get("inputs") or {}).get("partial_task_id")
                or task["task_id"]
            )
            batch = progress.batch_for_request(batch_request_id) if progress else None
            if batch:
                payload = {**payload, "batch_id": batch["batch_id"]}
                outcome = progress.complete(
                    batch_request_id,
                    payload,
                    result["receipt"]["response_sha256"],
                )
                final_payload = outcome.get("final_payload")
                submission = AgentHandoffService(self.runner).submit("footage", final_payload) if final_payload is not None else {
                    "accepted": True,
                    "stage": "footage",
                    "artifact": "footage_semantic_batch",
                    "remaining_batches": outcome["remaining_batches"],
                }
            else:
                submission = AgentHandoffService(self.runner).submit(str(task["stage"]), payload)
            resume = AgentBridgeService(self.runner).next()
            return {"submission": submission, "resume": resume}

        applied = self.exchange.apply(task, consume)
        applied_result = applied.get("result") or {}
        submission = applied_result.get("submission") or {}
        return {
            "status": "CONSUMED",
            "request_id": task["task_id"],
            "stage": task["stage"],
            "submission": submission,
            "resume": applied_result.get("resume"),
            "remaining_batches": submission.get("remaining_batches"),
        }

    def watch(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        while True:
            result = self.poll_once()
            if result is not None and result.get("status") != "WAITING_GPT_CORRECTION":
                return result
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for project mirror V2 response")
            self._sleep(self.config.poll_interval_seconds)

    def status(self) -> dict[str, Any]:
        current = self.transport.read_json("agent/current.json")
        task = self.exchange.store.read(str(current["task_id"])) if current else None
        return {
            "job_id": self.config.project_id,
            "remote_path": self.config.v2_remote_path,
            "active_request": task,
            "request_lifecycle": "waiting" if task else None,
            "remote": {"transport": "local_sync", "remote_path": str(self.transport.root)},
        }

    def _create_refinement(
        self,
        task: dict[str, Any],
        manifest: dict[str, Any],
        requests: list[dict[str, Any]],
        *,
        partial_result: dict[str, Any],
    ) -> dict[str, Any]:
        service = FootageRefinementService(self.runner)
        normalized = service.validate({"artifact": "footage_refinement_request", "requests": requests})
        sampled = service.sample(normalized, handoff_revision=int(task["revision"]) + 1)
        self.mirror.sync()
        inherited_partial = (task.get("inputs") or {}).get("partial_result") or {}
        combined_partial = self._merge_results(inherited_partial, partial_result)
        return self.exchange.create_refinement(
            task,
            manifest,
            normalized,
            task["result_schema"],
            partial_result=combined_partial,
            sampled_evidence=sampled,
        )

    def _validate_semantic_response(
        self, task: dict[str, Any], response: dict[str, Any]
    ) -> None:
        status = response["status"]
        payload = response["result"]
        if status == "needs_refinement":
            FootageRefinementService(self.runner).validate(
                {
                    "artifact": "footage_refinement_request",
                    "requests": payload["requests"],
                }
            )
            return
        stage = str(task["stage"])
        payload = self._effective_payload(task, payload)
        handoff = AgentHandoffService(self.runner)
        progress = self._footage_progress() if stage == "footage" else None
        batch_request_id = str(
            (task.get("inputs") or {}).get("partial_task_id")
            or task["task_id"]
        )
        batch = progress.batch_for_request(batch_request_id) if progress else None
        if batch:
            progress._validate_partial(
                batch, {**payload, "batch_id": batch["batch_id"]}
            )
            return
        handoff._validate(stage, payload)
        if stage == "proposal" and (payload.get("approval") or {}).get("status") not in {
            "approved",
            "approved_with_changes",
        }:
            raise ValueError("proposal requires recorded user approval")
        if stage in {"footage", "match"} and handoff._semantic_artifacts_ready(stage):
            validate_semantic_submission(self.runner, stage, payload)

    @classmethod
    def _effective_payload(
        cls, task: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        partial = (task.get("inputs") or {}).get("partial_result") or {}
        return cls._merge_results(partial, payload)

    @staticmethod
    def _merge_results(
        partial: dict[str, Any], update: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(partial)
        for key, value in update.items():
            if (
                key == "clips"
                and isinstance(result.get(key), list)
                and isinstance(value, list)
            ):
                by_id = {
                    str(item.get("clip_id") or item.get("asset_id") or index): item
                    for index, item in enumerate(result[key])
                    if isinstance(item, dict)
                }
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        identity = str(
                            item.get("clip_id") or item.get("asset_id") or index
                        )
                        by_id[identity] = item
                result[key] = list(by_id.values())
            elif isinstance(result.get(key), dict) and isinstance(value, dict):
                result[key] = ProjectMirrorBridge._merge_results(result[key], value)
            else:
                result[key] = value
        return result

    def _footage_progress(self) -> FootageSemanticProgress:
        existing = FootageSemanticProgress.open_existing(self.runner)
        if existing is not None:
            return existing
        return FootageSemanticProgress(
            self.runner,
            FootageBatchPolicy(
                # V2 is budget-led. These high structural ceilings only prevent
                # pathological unbounded collections; frames/bytes do the packing.
                max_clips=max(self.config.max_evidence_files, 1),
                max_refinement_ranges=max(self.config.max_evidence_files, 1),
                max_frames=min(self.config.max_footage_batch_frames, max(1, self.config.max_evidence_files - 2)),
                max_evidence_bytes=min(self.config.max_footage_batch_bytes, self.config.max_evidence_bytes),
            ),
        )

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        private_keys = {"project_root", "evidence_root", "local_path", "absolute_path"}

        def sanitize(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: sanitize(child)
                    for key, child in value.items()
                    if key not in private_keys
                    and not (
                        key.endswith("_path")
                        and isinstance(child, str)
                        and (Path(child).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", child))
                    )
                }
            if isinstance(value, list):
                return [sanitize(child) for child in value]
            return value

        return sanitize(task)

    @staticmethod
    def _asset_scope(manifest: dict[str, Any], stage: str) -> list[dict[str, Any]]:
        roles = {"reference"} if stage in {"proposal", "analyze"} else {"reference", "footage", "audio"}
        return [
            {
                "asset_id": item["asset_id"],
                "relative_path": item["relative_path"],
                "content_sha256": item["content_sha256"],
                "mirror_path": item["mirror_path"],
                "duration_seconds": item.get("duration_seconds"),
            }
            for item in manifest.get("assets", [])
            if item.get("role") in roles
        ]

    @staticmethod
    def _result_schema(
        contract: dict[str, Any], *, footage_batch: bool = False
    ) -> dict[str, Any]:
        schema = {
            key: value
            for key, value in contract.items()
            if key in {
                "$schema",
                "$defs",
                "type",
                "properties",
                "required",
                "additionalProperties",
                "allOf",
                "anyOf",
                "oneOf",
            }
        }
        schema.setdefault("type", "object")
        required = list(schema.get("required") or [])
        if not footage_batch:
            required = [item for item in required if item != "batch_id"]
        if required:
            schema["required"] = required
        return schema

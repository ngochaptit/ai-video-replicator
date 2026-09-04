from __future__ import annotations

import json

from moon import cli


def test_cli_frames_routes_through_registered_connector_path(monkeypatch, capsys) -> None:
    runner = object()
    observed = {}

    monkeypatch.setattr(cli, "_runner", lambda *args, **kwargs: runner)

    def fake_call(self, request):
        observed["runner"] = self.runner
        observed["request"] = request
        return {
            "evidence_registered": True,
            "stage": "footage",
            "sampling_group_id": "group-1",
        }

    monkeypatch.setattr(cli.AgentConnectorService, "call", fake_call)

    code = cli.main(
        [
            "--project",
            "D:/AI EDIT VIDEO/8.26-v12",
            "frames",
            "--source",
            "footage/oneshot.mp4",
            "--from",
            "2",
            "--to",
            "2.4",
            "--count",
            "2",
            "--width",
            "320",
        ]
    )

    assert code == 0
    assert observed["runner"] is runner
    assert observed["request"] == {
        "tool": "moon.frames.sample",
        "arguments": {
            "source": "footage/oneshot.mp4",
            "start_seconds": 2.0,
            "end_seconds": 2.4,
            "count": 2,
            "width": 320,
        },
    }
    output = json.loads(capsys.readouterr().out)
    assert output["evidence_registered"] is True
    assert output["stage"] == "footage"

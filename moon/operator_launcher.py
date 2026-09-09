from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from moon.operator import (
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    WAITING_AGENT,
    DuplicateProjectRun,
    OperatorWorkerProcess,
    WorkerLaunchError,
    inspect_operator_project,
    validate_operator_project,
)


BG = "#f4f6f8"
CARD = "#ffffff"
INK = "#17202a"
MUTED = "#667085"
BLUE = "#1769e0"
GREEN = "#12805c"
RED = "#c0362c"
AMBER = "#a35d00"
STATUS_COLORS = {
    PENDING: MUTED,
    RUNNING: BLUE,
    WAITING_AGENT: AMBER,
    COMPLETED: GREEN,
    FAILED: RED,
}


class OperatorLauncher(tk.Tk):
    def __init__(self, *, initial_project: str | None = None) -> None:
        super().__init__()
        self.title("AI Video Replicator")
        self.geometry("820x760")
        self.minsize(720, 650)
        self.configure(bg=BG)
        self.repository_root = Path(__file__).resolve().parents[1]
        self.worker = OperatorWorkerProcess(self.repository_root)
        self.project_var = tk.StringVar(value=initial_project or "")
        self.summary_var = tk.StringVar(value="Chọn thư mục dự án để bắt đầu.")
        self.final_var = tk.StringVar(value="")
        self.debug_visible = False
        self.stage_widgets: dict[str, tuple[ttk.Label, ttk.Label]] = {}
        self._build()
        self.after(350, self._refresh)

    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Segoe UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=("Segoe UI", 10))
        style.configure("Stage.TLabel", background=CARD, foreground=INK, font=("Segoe UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 12, "bold"), padding=(16, 12))
        style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))

        shell = ttk.Frame(self, padding=24)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="AI VIDEO REPLICATOR", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            shell,
            text="Chọn dự án, bấm bắt đầu, và theo dõi tiến trình tại đây.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 18))

        project_card = ttk.Frame(shell, style="Card.TFrame", padding=16)
        project_card.pack(fill="x")
        ttk.Label(project_card, text="Thư mục dự án", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(project_card, textvariable=self.project_var, state="readonly")
        entry.grid(row=1, column=0, sticky="ew", pady=(7, 0), padx=(0, 10))
        ttk.Button(project_card, text="CHỌN THƯ MỤC", command=self._choose_folder).grid(row=1, column=1, pady=(7, 0))
        project_card.columnconfigure(0, weight=1)

        self.start_button = ttk.Button(
            shell, text="START AI EDIT", style="Primary.TButton", command=self._start
        )
        self.start_button.pack(fill="x", pady=16)

        progress = ttk.Frame(shell, style="Card.TFrame", padding=16)
        progress.pack(fill="x")
        ttk.Label(progress, text="Tiến trình", style="Stage.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        for row, (stage, label) in enumerate(
            (
                ("proposal", "Proposal"),
                ("analyze", "Analyze"),
                ("footage", "Footage"),
                ("match", "Match"),
                ("timeline", "Timeline"),
                ("render", "Render"),
                ("qc", "QC"),
            ),
            start=1,
        ):
            stage_label = ttk.Label(progress, text=label, style="Card.TLabel")
            stage_label.grid(row=row, column=0, sticky="w", pady=4)
            status_label = ttk.Label(progress, text=PENDING, style="Card.TLabel")
            status_label.grid(row=row, column=1, sticky="e", pady=4)
            self.stage_widgets[stage] = (stage_label, status_label)
        progress.columnconfigure(0, weight=1)

        self.message = tk.Label(
            shell,
            textvariable=self.summary_var,
            bg=BG,
            fg=INK,
            justify="left",
            anchor="w",
            wraplength=740,
            font=("Segoe UI", 11),
        )
        self.message.pack(fill="x", pady=(14, 8))

        self.gemini_frame = ttk.Frame(shell, style="Card.TFrame", padding=16)
        tk.Label(
            self.gemini_frame,
            text="Cần phân tích bằng Gemini",
            bg=CARD,
            fg=AMBER,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            self.gemini_frame,
            text="Mở PDF bên dưới và tải duy nhất file này lên Gemini. Sau đó hệ thống sẽ tự tiếp tục khi nhận được phản hồi.",
            style="Card.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(5, 10))
        self.packet_button = ttk.Button(
            self.gemini_frame,
            text="MỞ FILE GỬI GEMINI",
            style="Action.TButton",
        )
        self.packet_button.pack(anchor="w")

        self.success_frame = ttk.Frame(shell, style="Card.TFrame", padding=18)
        tk.Label(
            self.success_frame,
            text="VIDEO ĐÃ HOÀN TẤT",
            bg=CARD,
            fg=GREEN,
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            self.success_frame,
            textvariable=self.final_var,
            style="Card.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(5, 12))
        actions = ttk.Frame(self.success_frame, style="Card.TFrame")
        actions.pack(anchor="w")
        self.open_video_button = ttk.Button(
            actions, text="OPEN FINAL VIDEO", style="Action.TButton"
        )
        self.open_video_button.pack(side="left", padx=(0, 8))
        self.open_folder_button = ttk.Button(
            actions, text="OPEN OUTPUT FOLDER", style="Action.TButton"
        )
        self.open_folder_button.pack(side="left")

        self.debug_toggle = ttk.Button(
            shell, text="Chi tiết kỹ thuật ▸", command=self._toggle_debug
        )
        self.debug_toggle.pack(anchor="w", pady=(12, 4))
        self.debug_frame = ttk.Frame(shell, style="Card.TFrame", padding=10)
        self.debug_text = tk.Text(
            self.debug_frame,
            height=10,
            wrap="word",
            bg="#101828",
            fg="#e4e7ec",
            insertbackground="white",
            font=("Consolas", 9),
            relief="flat",
        )
        self.debug_text.pack(fill="both", expand=True)
        self.debug_text.configure(state="disabled")

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Chọn thư mục dự án video")
        if selected:
            self.project_var.set(str(Path(selected).resolve()))
            self._refresh()

    def _start(self) -> None:
        value = self.project_var.get().strip()
        if not value:
            messagebox.showinfo("AI Video Replicator", "Hãy chọn thư mục dự án trước.")
            return
        validation = validate_operator_project(value)
        if not validation.valid:
            messagebox.showerror("Dự án chưa sẵn sàng", "\n".join(validation.errors))
            return
        try:
            self.worker.start(value)
        except (DuplicateProjectRun, WorkerLaunchError) as exc:
            messagebox.showinfo("AI Video Replicator", str(exc))
        self._refresh()

    def _refresh(self) -> None:
        project = self.project_var.get().strip()
        if not project:
            self.start_button.configure(state="disabled")
            self.after(1000, self._refresh)
            return
        snapshot = inspect_operator_project(project)
        self._render_snapshot(snapshot)
        process_failure = self.worker.failure()
        if process_failure and snapshot.get("status") not in {"failed", "complete"}:
            self.summary_var.set("Moon đã dừng ngoài dự kiến. Nhấn START AI EDIT để thử lại.")
            self.message.configure(fg=RED)
        self.after(1000, self._refresh)

    def _render_snapshot(self, snapshot: dict[str, Any]) -> None:
        status = snapshot.get("status")
        for item in snapshot.get("stages") or []:
            widgets = self.stage_widgets.get(str(item.get("stage")))
            if not widgets:
                continue
            stage_status = str(item.get("status") or PENDING)
            widgets[1].configure(
                text=stage_status, foreground=STATUS_COLORS.get(stage_status, MUTED)
            )
        self.summary_var.set(str(snapshot.get("message") or ""))
        self.message.configure(fg=RED if status == "failed" else INK)
        self.start_button.configure(
            state="normal" if snapshot.get("can_start") else "disabled"
        )

        packet = snapshot.get("portable_packet")
        if packet:
            self.packet_button.configure(command=lambda path=packet: self._open(path))
            if not self.gemini_frame.winfo_ismapped():
                self.gemini_frame.pack(fill="x", pady=(5, 8), before=self.debug_toggle)
        else:
            self.gemini_frame.pack_forget()

        final = Path(str(snapshot.get("final_path") or ""))
        if status == "complete" and final.is_file():
            self.final_var.set(str(final))
            self.open_video_button.configure(command=lambda: self._open(final))
            self.open_folder_button.configure(command=lambda: self._open(final.parent))
            if not self.success_frame.winfo_ismapped():
                self.success_frame.pack(fill="x", pady=(6, 8), before=self.debug_toggle)
        else:
            self.success_frame.pack_forget()
        self._render_debug(snapshot.get("debug") or {})

    def _toggle_debug(self) -> None:
        self.debug_visible = not self.debug_visible
        if self.debug_visible:
            self.debug_toggle.configure(text="Chi tiết kỹ thuật ▾")
            self.debug_frame.pack(fill="both", expand=True, pady=(0, 8))
        else:
            self.debug_toggle.configure(text="Chi tiết kỹ thuật ▸")
            self.debug_frame.pack_forget()

    def _render_debug(self, debug: dict[str, Any]) -> None:
        lines = ["MOON COMMANDS"]
        lines.extend(str(item) for item in debug.get("commands") or [])
        lines.extend(
            [
                "",
                f"REQUEST ID: {debug.get('request_id') or '-'}",
                f"STAGE INTERNALS: {debug.get('stage_internals') or '-'}",
                "",
                "STDOUT",
                str(debug.get("stdout") or ""),
                "",
                "STDERR",
                str(debug.get("stderr") or ""),
            ]
        )
        self.debug_text.configure(state="normal")
        self.debug_text.delete("1.0", "end")
        self.debug_text.insert("1.0", "\n".join(lines))
        self.debug_text.configure(state="disabled")

    @staticmethod
    def _open(path: str | Path) -> None:
        target = str(Path(path).resolve())
        try:
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError as exc:
            messagebox.showerror("Không thể mở", str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Video Replicator operator launcher")
    parser.add_argument("--project")
    args = parser.parse_args(argv)
    app = OperatorLauncher(initial_project=args.project)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

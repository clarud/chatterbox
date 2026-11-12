"""Workspace/session controller helpers; engine modules do not import this UI code."""
from datetime import datetime, timezone
from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from .destination import collect_destination
from .history import HistoryStore
from .pipeline import State
from .transcripts import TranscriptStore


class SessionsMixin:
    def _init_history(self):
        directory = Path(self.store.directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.history = HistoryStore(directory / "history.sqlite3")
        self.transcripts = TranscriptStore(directory / "transcripts.sqlite3")
        self.active_session = self.history.get_active_session()
        self.selected_transcript = None
        self.dictation_context = {}
        self.history_error = ""

    def _freeze_context(self, target):
        self.dictation_context = {"chat_session_id": self.active_session,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": ("Local Whisper" + (" + " + self.settings.provider.title() if self.session_mode == "polished" else ""))
                        if self.settings.transcription_backend == "local" else self.settings.provider,
            **collect_destination(target, self.settings.track_application, self.settings.collect_window_title)}

    def _persist_result(self, result):
        session = self.dictation_context.get("chat_session_id")
        if session is None:  # Quick Dictation never inserts history or invents a session.
            return
        try:
            self.history.upsert_dictation(session, result.to_dict(), self.dictation_context)
            if self.active_session == session:
                self.selected_transcript = result.session_id
            self.view.workspace.refresh()
        except Exception as exc:
            self.history_error = "History could not be saved: " + str(exc)
            # History failure must not silently cancel or duplicate an in-flight paste.
            self.view.workspace.note.set(self.history_error + ". Keep the preview or use Copy.")

    def _new_session(self):
        if self.is_busy():
            return
        try:
            self._select_session(self.history.create_session())
        except Exception as exc:
            self._set_state(State.ERROR, str(exc))

    def _select_session(self, session_id):
        if self.is_busy():
            self.view.workspace.refresh_sessions()
            return False
        try:
            self.history.set_active_session(session_id)
            self.active_session = session_id
            self.selected_transcript = None
            if self.view.page == "settings":
                self.view.toggle_settings()
            self._preview("")
            self.view.workspace.refresh()
            self.view.state_changed(self.state, self.is_busy())
            return True
        except Exception as exc:
            self._set_state(State.ERROR, str(exc))
            return False

    def _rename_session(self):
        if self.is_busy() or not self.active_session:
            return
        current = next((row for row in self.history.list_sessions() if row["id"] == self.active_session), None)
        if not current:
            return
        title = simpledialog.askstring("Rename session", "Session name:", initialvalue=current["title"], parent=self.root)
        if title is not None:
            try:
                self.history.rename_session(self.active_session, title)
                self.view.workspace.refresh()
            except Exception as exc:
                self._set_state(State.ERROR, str(exc))

    def _delete_session(self):
        if self.is_busy() or not self.active_session:
            return
        current = next((row for row in self.history.list_sessions() if row["id"] == self.active_session), None)
        if current and messagebox.askyesno("Delete session", f'Delete “{current["title"]}” and its {current["dictation_count"]} dictations?\nThis cannot be undone. Audio and API keys are not affected.', parent=self.root):
            try:
                self.history.delete_session(self.active_session)
                self.active_session = None
                self.selected_transcript = None
                self._preview("")
                self.view.workspace.refresh()
                self.view.state_changed(self.state, self.is_busy())
            except Exception as exc:
                self._set_state(State.ERROR, str(exc))

    def _review_transcript(self, transcript_id, version=None):
        row = self.history.get_dictation(transcript_id) if transcript_id else None
        self.selected_transcript = transcript_id if row else None
        if not row:
            return
        available = ["Raw"]
        if row.get("polished") is not None:
            available.append("Polished")
        if row.get("edited_text") is not None:
            available.append("Edited")
        selected = version if version in available else available[-1]
        self.view.workspace.version_combo.configure(values=available)
        self.view.workspace.version.set(selected)
        key = {"Raw": "raw", "Polished": "polished", "Edited": "edited_text"}[selected]
        self._preview(row.get(key) or "")
        self.view.workspace.show_detail(row)
        self.view.state_changed(self.state, self.is_busy())

    def _edit_transcript(self):
        if self.is_busy() or not self.selected_transcript:
            return
        row = self.history.get_dictation(self.selected_transcript)
        if not row:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit saved text")
        dialog.transient(self.root)
        dialog.geometry("600x430")
        palette = self.view.palette
        dialog.configure(bg=palette["bg"])
        tk.Label(dialog, text="Original Raw and Polished versions will be preserved.", fg=palette["muted"], bg=palette["bg"], padx=20, pady=14).pack(anchor="w")
        editor = tk.Text(dialog, wrap="word", font=("Segoe UI", 11), bg=palette["surface"], fg=palette["text"], insertbackground=palette["text"], bd=0, padx=18, pady=15)
        editor.pack(fill="both", expand=True, padx=20)
        editor.insert("1.0", row.get("edited_text") if row.get("edited_text") is not None else row.get("polished") or row["raw"])
        actions = tk.Frame(dialog, bg=palette["bg"])
        actions.pack(fill="x", padx=20, pady=14)
        def save():
            try:
                self.history.edit_dictation(row["id"], editor.get("1.0", "end-1c"))
                dialog.destroy()
                self.view.workspace.refresh()
                self._review_transcript(row["id"], "Edited")
            except Exception as exc:
                messagebox.showerror("Could not save edit", str(exc), parent=dialog)
        from .ui import RoundedButton
        for title, callback, primary in (("Save edit", save, True), ("Cancel", dialog.destroy, False)):
            button = RoundedButton(actions, text=title, command=callback, width=130, height=38, primary=primary)
            button.apply_theme(palette)
            button.pack(side="left", padx=(0, 8))
        editor.focus_set()
        dialog.grab_set()

    def _delete_transcript(self):
        if self.is_busy() or not self.selected_transcript:
            return
        if messagebox.askyesno("Delete dictation", "Delete this saved dictation, including its raw, polished and edited versions?\nThis cannot be undone.", parent=self.root):
            try:
                self.history.delete_dictation(self.selected_transcript)
                self.selected_transcript = None
                self._preview("")
                self.view.workspace.refresh()
            except Exception as exc:
                self._set_state(State.ERROR, str(exc))

    def _export_session(self, format="md"):
        if self.is_busy() or not self.active_session:
            return
        current = next((row for row in self.history.list_sessions() if row["id"] == self.active_session), None)
        if not current:
            return
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", current["title"]).strip(" .") or "session"
        if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            stem = "session-" + stem
        path = filedialog.asksaveasfilename(parent=self.root, title="Export session", initialfile=stem + "." + format,
            defaultextension="." + format, filetypes=[("Markdown" if format == "md" else "Text", "*." + format)])
        if path:
            try:
                Path(path).write_text(self.history.export_session(self.active_session, format), encoding="utf-8")
                self.message.set("Session exported. Original history was kept.")
            except Exception as exc:
                self._set_state(State.ERROR, "Export failed: " + str(exc))

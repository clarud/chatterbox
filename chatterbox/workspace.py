"""Optional session workspace. No microphone, API, focus, or insertion code."""
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk


def _relative_time(iso):
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    seconds = (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return when.astimezone().strftime("%d %b")


class WorkspaceView:
    def __init__(self, view, sidebar):
        self.view, self.app, self.sidebar = view, view.app, sidebar
        self.ids, self.rows, self.bubbles, self.bubble_labels, self.thread_wraplabels = [], {}, {}, {}, []
        self.refreshing, self.search_timer = False, None
        self.filters_visible = False
        self.sidebar_labels, self.sidebar_buttons = [], []

        new_session = view.button(sidebar, text="+ New session", command=self.app._new_session, width=180, height=42, primary=True)
        new_session.pack(fill="x", pady=(0, 6))
        self.sidebar_buttons.append(new_session)
        quick = view.button(sidebar, text="Quick Dictation", command=lambda: self.app._select_session(None), icon_name="mic", width=180, height=38)
        quick.pack(fill="x", pady=(0, 6))
        self.sidebar_buttons.append(quick)
        transcribe_file = view.button(sidebar, text="Transcribe file…", command=self.app._transcribe_file, width=180, height=38)
        transcribe_file.pack(fill="x", pady=(0, 16))
        self.sidebar_buttons.append(transcribe_file)

        label = view.label(sidebar, text="Recent Sessions", role="muted", font=("Segoe UI", 9))
        label.pack(anchor="w", padx=7, pady=(0, 8))
        self.sidebar_labels.append(label)

        settings_button = view.button(sidebar, text="Settings", command=view.toggle_settings, icon_name="settings", width=180, height=38)
        settings_button.pack(side="bottom", fill="x", pady=(10, 0))
        self.sidebar_buttons.append(settings_button)

        session_list = view.frame(sidebar)
        session_list.pack(fill="both", expand=True)
        self.sessions = tk.Listbox(session_list, bd=0, highlightthickness=0, relief="flat", activestyle="none",
                                  exportselection=False, selectmode="browse", font=("Segoe UI", 10), width=22)
        self.sessions.pack(side="left", fill="both", expand=True)
        self.session_scrollbar = ttk.Scrollbar(session_list, orient="vertical", command=self.sessions.yview, style="Quiet.Vertical.TScrollbar")
        self.session_scrollbar.pack(side="right", fill="y")
        self.sessions.configure(yscrollcommand=self.session_scrollbar.set)
        self.sessions.bind("<<ListboxSelect>>", self._session_selected)
        self.sessions.bind("<Button-3>", self._session_context_menu)
        self.session_menu = tk.Menu(self.sessions, tearoff=0)
        self.session_menu.add_command(label="Rename", command=self.app._rename_session)
        self.session_menu.add_command(label="Export as Markdown", command=lambda: self.app._export_session("md"))
        self.session_menu.add_command(label="Export as text", command=lambda: self.app._export_session("txt"))
        self.session_menu.add_separator()
        self.session_menu.add_command(label="Delete", command=self.app._delete_session)

        # File Transcripts: past uploaded-file transcriptions, kept separate from
        # dictation sessions since they're a different kind of content (existing
        # recordings fed in, not live dictation).
        self.file_transcript_ids = []
        transcripts_label = view.label(sidebar, text="File Transcripts", role="muted", font=("Segoe UI", 9))
        transcripts_label.pack(anchor="w", padx=7, pady=(10, 8))
        self.sidebar_labels.append(transcripts_label)
        transcript_list = view.frame(sidebar)
        transcript_list.pack(fill="x")
        self.file_transcripts = tk.Listbox(transcript_list, bd=0, highlightthickness=0, relief="flat", activestyle="none",
                                           exportselection=False, selectmode="browse", font=("Segoe UI", 10), width=22, height=6)
        self.file_transcripts.pack(side="left", fill="both", expand=True)
        self.file_transcript_scrollbar = ttk.Scrollbar(transcript_list, orient="vertical", command=self.file_transcripts.yview, style="Quiet.Vertical.TScrollbar")
        self.file_transcript_scrollbar.pack(side="right", fill="y")
        self.file_transcripts.configure(yscrollcommand=self.file_transcript_scrollbar.set)
        self.file_transcripts.bind("<<ListboxSelect>>", self._file_transcript_selected)
        self.file_transcripts.bind("<Button-3>", self._file_transcript_context_menu)
        self.file_transcript_menu = tk.Menu(self.file_transcripts, tearoff=0)
        self.file_transcript_menu.add_command(label="Open", command=self._open_selected_file_transcript)
        self.file_transcript_menu.add_command(label="Delete", command=self._delete_selected_file_transcript)

        # Main panel ---------------------------------------------------------------
        self.heading = view.frame(view.main_page)
        self.heading.pack(fill="x", before=view.card, pady=(0, 12))
        header_row = view.frame(self.heading)
        header_row.pack(fill="x")
        self.title = view.label(header_row, text="Quick Dictation", font=("Segoe UI", 18, "bold"))
        self.title.pack(side="left", anchor="w")
        self.filter_button = view.button(header_row, text="Filter", command=self._toggle_filters, width=74, height=32)
        self.filter_button.pack(side="right")
        self.note = tk.StringVar(value="Not saved to history. Your workspace can stay hidden.")
        view.label(self.heading, textvariable=self.note, role="muted", font=("Segoe UI", 9), wraplength=600, justify="left").pack(anchor="w", pady=(5, 0))

        self.history_panel = view.frame(view.main_page)

        self.filters_row = view.frame(self.history_panel)
        self.search = tk.StringVar()
        self.since, self.until, self.application = tk.StringVar(), tk.StringVar(), tk.StringVar(value="All applications")
        search_row = view.frame(self.filters_row)
        search_row.pack(fill="x", pady=(0, 6))
        view.label(search_row, text="Search", role="muted", font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        self.search_entry = ttk.Entry(search_row, textvariable=self.search, style="Quiet.TEntry")
        self.search_entry.pack(side="left", fill="x", expand=True)
        date_row = view.frame(self.filters_row)
        date_row.pack(fill="x", pady=(0, 10))
        view.label(date_row, text="Dates (UTC)", role="muted", font=("Segoe UI", 8)).pack(side="left", padx=(0, 7))
        for label_text, variable in (("From", self.since), ("To", self.until)):
            view.label(date_row, text=label_text, role="muted", font=("Segoe UI", 8)).pack(side="left", padx=(4, 3))
            ttk.Entry(date_row, textvariable=variable, style="Quiet.TEntry", width=10).pack(side="left")
        self.application_combo = ttk.Combobox(date_row, textvariable=self.application, values=("All applications",),
            style="Quiet.TCombobox", state="readonly", width=17)
        self.application_combo.pack(side="right")
        self.application_combo.bind("<<ComboboxSelected>>", lambda event: self._schedule_search())
        for variable in (self.search, self.since, self.until):
            variable.trace_add("write", lambda *args: self._schedule_search())

        thread_frame = view.frame(self.history_panel, surface=True)
        thread_frame.pack(fill="both", expand=True, pady=(0, 7))
        self.thread_area = view.scroll_area(thread_frame, on_width=self._resize_thread)

        review = view.frame(self.history_panel)
        review.pack(fill="x")
        self.version = tk.StringVar(value="Raw")
        self.version_combo = ttk.Combobox(review, textvariable=self.version, values=("Raw", "Polished", "Edited"),
            state="readonly", style="Quiet.TCombobox", width=10)
        self.version_combo.pack(side="left")
        self.version_combo.bind("<<ComboboxSelected>>", lambda event: self.app._review_transcript(self.app.selected_transcript, self.version.get()))
        self.detail = tk.StringVar()
        self.detail_label = view.label(review, textvariable=self.detail, role="muted", font=("Segoe UI", 8), wraplength=210, justify="left")
        self.detail_label.pack(side="left", padx=9, fill="x", expand=True)
        review.bind("<Configure>", lambda event: self.detail_label.configure(wraplength=max(120, event.width - 230)))
        self.detail_buttons = []
        for text, callback in (("Edit", self.app._edit_transcript), ("Delete", self.app._delete_transcript)):
            button = view.button(review, text=text, command=callback, width=70, height=32)
            button.pack(side="right")
            self.detail_buttons.append(button)

    def _session_selected(self, event):
        if not self.refreshing and self.sessions.curselection():
            self.app._select_session(self.ids[self.sessions.curselection()[0]])

    def _session_context_menu(self, event):
        index = self.sessions.nearest(event.y)
        if not (0 <= index < len(self.ids)):
            return
        self.sessions.selection_clear(0, "end")
        self.sessions.selection_set(index)
        if self.app._select_session(self.ids[index]):
            self.session_menu.tk_popup(event.x_root, event.y_root)

    def _file_transcript_selected(self, event):
        if not self.refreshing and self.file_transcripts.curselection():
            self._open_file_transcript(self.file_transcript_ids[self.file_transcripts.curselection()[0]])

    def _file_transcript_context_menu(self, event):
        index = self.file_transcripts.nearest(event.y)
        if not (0 <= index < len(self.file_transcript_ids)):
            return
        self.file_transcripts.selection_clear(0, "end")
        self.file_transcripts.selection_set(index)
        self.file_transcript_menu.tk_popup(event.x_root, event.y_root)

    def _open_selected_file_transcript(self):
        selection = self.file_transcripts.curselection()
        if selection:
            self._open_file_transcript(self.file_transcript_ids[selection[0]])

    def _open_file_transcript(self, transcript_id):
        row = self.app.transcripts.get(transcript_id)
        if row:
            self.view.show_file_transcript(row["source_name"], row["segments"])

    def _delete_selected_file_transcript(self):
        selection = self.file_transcripts.curselection()
        if selection:
            self.app.transcripts.delete(self.file_transcript_ids[selection[0]])
            self.refresh_file_transcripts()

    def refresh_file_transcripts(self):
        self.refreshing = True
        try:
            rows = self.app.transcripts.list()
            self.file_transcript_ids = [row["id"] for row in rows]
            self.file_transcripts.delete(0, "end")
            for row in rows:
                when = _relative_time(row["created_at"])
                self.file_transcripts.insert("end", f'{row["source_name"]} · {when}' if when else row["source_name"])
        finally:
            self.refreshing = False

    def _toggle_filters(self):
        self.filters_visible = not self.filters_visible
        if self.filters_visible:
            self.filters_row.pack(fill="x", before=self.thread_area.canvas.master, pady=(0, 10))
        else:
            self.filters_row.pack_forget()

    def _schedule_search(self):
        if self.refreshing:
            return
        if self.search_timer:
            self.view.root.after_cancel(self.search_timer)
        self.search_timer = self.view.root.after(250, self._search_now)

    def _search_now(self):
        self.search_timer = None
        if self.app.closing:
            return
        try:
            self.refresh_history()
        except Exception as exc:
            self.note.set("Filter error: " + str(exc) + ". Use YYYY-MM-DD dates.")

    def refresh_sessions(self):
        self.refreshing = True
        try:
            rows = self.app.history.list_sessions()
            self.ids = [row["id"] for row in rows]
            self.sessions.delete(0, "end")
            for row in rows:
                when = _relative_time(row["updated_at"])
                self.sessions.insert("end", f'{row["title"]} · {when}' if when else row["title"])
            if self.app.active_session in self.ids:
                self.sessions.selection_set(self.ids.index(self.app.active_session))
        finally:
            self.refreshing = False

    def refresh(self):
        self.refresh_sessions()
        self.refresh_file_transcripts()
        active = self.app.active_session
        session = next((row for row in self.app.history.list_sessions() if row["id"] == active), None)
        if session:
            self.title.configure(text=session["title"])
            self.note.set(f'{session["dictation_count"]} dictations · saved locally as text')
            self.history_panel.pack(fill="both", expand=True, before=self.view.card, pady=(0, 12))
            self.application_combo.configure(values=("All applications", *self.app.history.applications(active)))
            self.refresh_history()
        else:
            self.title.configure(text="Quick Dictation")
            self.note.set("Not saved to history. Your workspace can stay hidden.")
            self.history_panel.pack_forget()
        self.view.state_changed(self.app.state, self.app.is_busy())

    def refresh_history(self):
        if not self.app.active_session:
            return
        query = self.search.get().strip()
        rows = self.app.history.list_dictations(self.app.active_session, query=query,
            since=self.since.get().strip() or None, until=self.until.get().strip() or None,
            application=None if self.application.get() == "All applications" else self.application.get(), limit=200, recent=True)
        self.refreshing = True
        try:
            self.rows = {row["id"]: row for row in rows}
            for child in self.thread_area.body.winfo_children():
                child.destroy()
            self.bubbles, self.bubble_labels, self.thread_wraplabels = {}, {}, []
            palette = self.view.palette
            for row in rows:
                bubble, meta_label, text_label = self._build_bubble(row, palette)
                bubble.pack(fill="x", padx=6, pady=(0, 10))
                self.bubbles[row["id"]] = bubble
                self.bubble_labels[row["id"]] = (meta_label, text_label)
            self._resize_thread(self.thread_area.canvas.winfo_width())
            selected = self.app.selected_transcript
            if selected not in self.rows and rows:
                selected = rows[-1]["id"]
            if selected in self.rows:
                self._select_bubble(selected)
                self.app._review_transcript(selected)
            elif not rows:
                self.app.selected_transcript = None
                self.detail.set("No matching dictations. Dates use YYYY-MM-DD.")
                self.app._preview("")
            self.thread_area.scroll_to_bottom()
        finally:
            self.refreshing = False
        if len(rows) >= 200:
            self.note.set("Showing the newest 200 matches in chronological order. Filter or export the full session.")

    def _build_bubble(self, row, palette):
        text = row.get("edited_text") if row.get("edited_text") is not None else row.get("polished") or row["raw"]
        when = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%d %b  %H:%M")
        meta = f'{when} · {row.get("application") or "Unknown app"}'
        frame = tk.Frame(self.thread_area.body, bg=palette["surface"], highlightthickness=1, highlightbackground=palette["border"])
        meta_label = tk.Label(frame, text=meta, fg=palette["muted"], bg=palette["surface"], font=("Segoe UI", 8), anchor="w", bd=0)
        meta_label.pack(fill="x", padx=14, pady=(10, 2))
        # A disabled Text (not Label) so the transcript can be mouse-selected and copied in place.
        text_widget = tk.Text(frame, wrap="word", font=("Segoe UI", 11), bd=0, highlightthickness=0,
                              padx=0, pady=0, height=1, width=1, cursor="xterm", exportselection=False,
                              bg=palette["surface"], fg=palette["text"], insertbackground=palette["text"],
                              selectbackground=palette["selection"], selectforeground=palette["text"])
        text_widget.insert("1.0", text)
        text_widget.configure(state="disabled")
        text_widget.pack(fill="x", padx=14, pady=(0, 12))
        self.thread_wraplabels.append(text_widget)
        meta_label.bind("<Button-1>", lambda event, rid=row["id"]: self._bubble_clicked(rid))
        frame.bind("<Button-1>", lambda event, rid=row["id"]: self._bubble_clicked(rid))
        text_widget.bind("<ButtonRelease-1>", lambda event, rid=row["id"]: self._bubble_clicked(rid))
        return frame, meta_label, text_widget

    def _resize_thread(self, width):
        self.thread_area.body.update_idletasks()
        for widget in self.thread_wraplabels:
            lines = widget.count("1.0", "end-1c", "displaylines")
            widget.configure(height=max(1, lines[0] if lines else 1))

    def _bubble_clicked(self, rid):
        if self.refreshing or self.app.is_busy():
            return
        self._select_bubble(rid)
        self.app._review_transcript(rid)

    def _select_bubble(self, rid):
        palette = self.view.palette
        for bid, frame in self.bubbles.items():
            selected = bid == rid
            frame.configure(highlightbackground=palette["selection"] if selected else palette["border"],
                            highlightthickness=2 if selected else 1)

    def clear_filters(self):
        self.refreshing = True
        try:
            self.search.set("")
            self.since.set("")
            self.until.set("")
            self.application.set("All applications")
        finally:
            self.refreshing = False

    def show_detail(self, row):
        timing = []
        for title, key in (("STT", "transcription_seconds"), ("Polish", "refinement_seconds")):
            value = row.get(key)
            timing.append(f"{title} {value:.2f}s" if value is not None else f"{title} skipped/unknown")
        outcome = "check destination" if row.get("possibly_sent") else "paste sent" if row.get("insertion_sent") else "not sent"
        self.detail.set(f'{row["mode"].title()} · {row.get("provider") or "Unknown provider"} · {outcome}\n' + " · ".join(timing))

    def recovery_visible(self):
        result = self.app.last_result
        if not result:
            return False
        if self.app.active_session != self.app.dictation_context.get("chat_session_id"):
            return False
        return self.app.active_session is None or self.app.selected_transcript == result.session_id

    def state_changed(self, busy):
        for button in self.sidebar_buttons[:3]:
            button.configure(state="disabled" if busy else "normal")
        for button in self.detail_buttons:
            button.configure(state="disabled" if busy or not self.app.selected_transcript else "normal")
        self.version_combo.configure(state="disabled" if busy or not self.app.selected_transcript else "readonly")

    def apply_theme(self, palette):
        sidebar_palette = dict(palette, bg="#EFEFEC" if self.app.settings.theme == "light" else "#151515")
        self.view.sidebar.configure(bg=sidebar_palette["bg"])
        self.sidebar.configure(bg=sidebar_palette["bg"])
        for label in self.sidebar_labels:
            label.configure(bg=sidebar_palette["bg"])
        for button in self.sidebar_buttons:
            button.apply_theme(sidebar_palette)
        self.sessions.configure(bg=sidebar_palette["bg"], fg=palette["text"], selectbackground=palette["selection"],
                                selectforeground=palette["text"])
        self.file_transcripts.configure(bg=sidebar_palette["bg"], fg=palette["text"], selectbackground=palette["selection"],
                                        selectforeground=palette["text"])
        self.thread_area.canvas.configure(bg=palette["bg"])
        self.thread_area.body.configure(bg=palette["bg"])
        for rid, frame in self.bubbles.items():
            selected = rid == self.app.selected_transcript
            frame.configure(bg=palette["surface"],
                            highlightbackground=palette["selection"] if selected else palette["border"])
            meta_label, text_widget = self.bubble_labels[rid]
            meta_label.configure(bg=palette["surface"], fg=palette["muted"])
            text_widget.configure(bg=palette["surface"], fg=palette["text"], insertbackground=palette["text"],
                                  selectbackground=palette["selection"], selectforeground=palette["text"])

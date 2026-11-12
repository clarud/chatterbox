"""Native Tk presentation layer: quiet main screen, separate settings, two themes."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk

from .pipeline import State
from .settings import PROVIDER_NAMES
from .windows import set_titlebar_theme

PALETTES = {
    "light": {"bg": "#F7F7F5", "surface": "#FFFFFF", "border": "#E3E3DF", "text": "#262623",
              "muted": "#777773", "hint": "#A4A49F", "hover": "#EAEAE6", "primary": "#292927",
              "primary_hover": "#40403C", "on_primary": "#FFFFFF", "selection": "#DEDEDA",
              "error": "#B53D3D", "ready": "#6D8775", "processing": "#9B8048"},
    "dark": {"bg": "#191919", "surface": "#222222", "border": "#363635", "text": "#EEEEEA",
             "muted": "#A2A29C", "hint": "#74746F", "hover": "#2D2D2B", "primary": "#E8E8E3",
             "primary_hover": "#D0D0CA", "on_primary": "#20201F", "selection": "#41413E",
             "error": "#F08B8B", "ready": "#91AC99", "processing": "#CEB782"},
}


def rounded(canvas, x1, y1, x2, y2, radius, **options):
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


def icon(canvas, name, x, y, color):
    """Small vector control icons; no image/font dependencies."""
    if name == "mic":
        rounded(canvas, x - 4, y - 9, x + 4, y + 3, 4, fill="", outline=color, width=1.6)
        canvas.create_arc(x - 8, y - 5, x + 8, y + 9, start=180, extent=180, style="arc", outline=color, width=1.6)
        canvas.create_line(x, y + 9, x, y + 13, fill=color, width=1.6)
        canvas.create_line(x - 4, y + 13, x + 4, y + 13, fill=color, width=1.6)
    elif name == "stop":
        rounded(canvas, x - 6, y - 6, x + 6, y + 6, 2, fill=color, outline="")
    elif name == "sun":
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, outline=color, width=1.5)
        for angle in range(0, 360, 45):
            a = math.radians(angle)
            canvas.create_line(x + 7 * math.cos(a), y + 7 * math.sin(a), x + 10 * math.cos(a), y + 10 * math.sin(a), fill=color, width=1.5)
    elif name == "moon":
        canvas.create_arc(x - 8, y - 8, x + 8, y + 8, start=65, extent=275, style="arc", outline=color, width=1.6)
        canvas.create_arc(x - 1, y - 9, x + 12, y + 4, start=155, extent=170, style="arc", outline=color, width=1.6)
    elif name == "settings":
        canvas.create_oval(x - 6, y - 6, x + 6, y + 6, outline=color, width=1.5)
        canvas.create_oval(x - 2, y - 2, x + 2, y + 2, outline=color, width=1.4)
        for angle in range(0, 360, 45):
            a = math.radians(angle)
            canvas.create_line(x + 6 * math.cos(a), y + 6 * math.sin(a), x + 9 * math.cos(a), y + 9 * math.sin(a), fill=color, width=2)
    elif name == "back":
        canvas.create_line(x + 4, y - 6, x - 3, y, x + 4, y + 6, fill=color, width=1.7)


class ScrollableArea:
    """Canvas+frame+scrollbar+mousewheel area; ``.body`` holds the content.

    Mousewheel only scrolls while the pointer is over this area (Enter/Leave
    bound), so it never fights another scrollable area on the same page.
    """

    def __init__(self, view, parent, on_width=None):
        self.canvas = tk.Canvas(parent, bd=0, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview, style="Quiet.Vertical.TScrollbar")
        self.scrollbar.pack(side="right", fill="y", padx=(8, 0))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = view.frame(self.canvas)
        self.window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.on_width = on_width
        self.body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<Enter>", lambda event: self.canvas.bind_all("<MouseWheel>", self._scroll))
        self.canvas.bind("<Leave>", lambda event: self.canvas.unbind_all("<MouseWheel>"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)
        if self.on_width:
            self.on_width(event.width)

    def _scroll(self, event):
        self.canvas.yview_scroll((-1 if event.delta > 0 else 1) * max(1, abs(event.delta) // 120), "units")

    def scroll_to_bottom(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text="", command=None, icon_name=None, width=160, height=44, primary=False):
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0, takefocus=1)
        self.text, self.command, self.icon_name = text, command, icon_name
        self.primary, self.state = primary, "normal"
        self.palette = PALETTES["light"]
        self.hover, self.focused, self.pressed = False, False, False
        self.tooltip_text, self.tooltip, self.tooltip_timer = "", None, None
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<FocusIn>", lambda event: self._focus(True))
        self.bind("<FocusOut>", lambda event: self._focus(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda event: self.invoke())
        self.bind("<space>", lambda event: self.invoke())
        self.bind("<Configure>", lambda event: self.draw())
        self._min_width = width
        self.draw()

    def configure(self, cnf=None, **options):
        if cnf:
            options.update(cnf)
        if "text" in options:
            self.text = options.pop("text")
        if "state" in options:
            self.state = options.pop("state")
        if "icon_name" in options:
            self.icon_name = options.pop("icon_name")
        if "command" in options:
            self.command = options.pop("command")
        if options:
            super().configure(**options)
        self.draw()

    config = configure

    def cget(self, option):
        if option in ("state", "text"):
            return getattr(self, option)
        return super().cget(option)

    __getitem__ = cget

    def invoke(self):
        if self.state != "disabled" and self.command:
            self.command()
        return "break"

    def _press(self, event):
        if self.state != "disabled":
            self.focus_set()
            self.pressed = True

    def _release(self, event):
        if self.pressed and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()
        self.pressed = False

    def _focus(self, focused):
        self.focused = focused
        self.draw()

    def _enter(self, event):
        self.hover = True
        self.draw()
        if self.tooltip_text:
            self.tooltip_timer = self.after(600, self._show_tooltip)

    def _leave(self, event):
        self.hover = False
        self.pressed = False
        self.draw()
        self._hide_tooltip()

    def _show_tooltip(self):
        self.tooltip_timer = None
        if not self.winfo_exists() or not self.hover:
            return
        self.tooltip = tk.Toplevel(self)
        self.tooltip.overrideredirect(True)
        self.tooltip.geometry(f"+{self.winfo_rootx()}+{self.winfo_rooty() + self.winfo_height() + 5}")
        tk.Label(self.tooltip, text=self.tooltip_text, bg=self.palette["surface"], fg=self.palette["text"],
                 font=("Segoe UI", 9), padx=10, pady=6, relief="solid", bd=1).pack()

    def _hide_tooltip(self):
        if self.tooltip_timer:
            self.after_cancel(self.tooltip_timer)
            self.tooltip_timer = None
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

    def apply_theme(self, palette):
        self.palette = palette
        self._hide_tooltip()
        self.draw()

    def draw(self):
        if not hasattr(self, "palette"):
            return
        p = self.palette
        self.delete("all")
        super().configure(bg=p["bg"], cursor="arrow" if self.state == "disabled" else "hand2")
        measured = int(self.tk.call("font", "measure", ("Segoe UI", 11, "bold"), self.text)) if self.text else 0
        # DPI scaling enlarges point-sized fonts but not a canvas's literal pixel width,
        # so a fixed width picked at 100% scaling can clip the label at higher scaling.
        needed = measured + (58 if self.icon_name else 32)
        width = max(self.winfo_width(), self._min_width, needed)
        if int(self.cget("width")) < width:
            super().configure(width=width)
        height = max(self.winfo_height(), int(self.cget("height")))
        fill = p["primary_hover"] if self.hover else p["primary"]
        fg = p["on_primary"] if self.primary else p["text"]
        if not self.primary:
            fill = p["hover"] if self.hover else p["bg"]
        if self.state == "disabled":
            fill, fg = p["hover"], p["muted"]
        rounded(self, 2, 2, width - 2, height - 2, 12, fill=fill,
                outline=p["muted"] if self.focused else "", width=1)
        text_x = width / 2 + (13 if self.icon_name and self.text else 0)
        if self.icon_name:
            icon_x = (width - measured - 26) / 2 + 8 if self.text else width / 2
            icon(self, self.icon_name, icon_x, height / 2 - 1, fg)
        if self.text:
            self.create_text(text_x, height / 2, text=self.text, fill=fg, font=("Segoe UI", 11, "bold"))


class DesktopView:
    """Presentation-only widgets. The controller owns recording/API/focus logic."""
    EXPORTED = ("hotkey_var", "transcribe_var", "refine_var", "provider_var", "provider_combo",
                "setting_widgets", "key_status", "api_url", "privacy_message", "preview",
                "state_label", "record_button", "cancel_button", "mode_var", "mode_combo",
                "overlay_position_var", "overlay_margin_var", "tray_status", "insert_raw_button",
                "backend_var", "refine_backend_var", "local_model_var", "tracking_var", "title_tracking_var", "local_status",
                "latency_status", "retry_button")

    def __init__(self, app):
        self.app, self.root = app, app.root
        self.page = "main"
        self.frames, self.labels, self.buttons = [], [], []
        self.palette = PALETTES[app.settings.theme]
        # DPI scaling enlarges fonts/content but not literal pixel sizes (see
        # RoundedButton.draw()); scale these floors the same way so a small
        # minimum window on a high-DPI display still fits the real content.
        dpi_scale = self.root.tk.call("tk", "scaling") / 1.333333
        width, height = min(1020, self.root.winfo_screenwidth() - 70), min(740, self.root.winfo_screenheight() - 90)
        self.root.geometry(f"{max(680, width)}x{max(480, height)}")
        self.root.minsize(round(min(800, max(620, width)) * dpi_scale), round(min(560, max(440, height)) * dpi_scale))
        self.root.option_add("*Font", ("Segoe UI", 10))
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.sidebar = self.frame(self.root, width=210)
        self.sidebar.pack(side="left", fill="y", padx=(0, 1))
        self.sidebar_body = self.frame(self.sidebar)
        self.sidebar_body.pack(fill="both", expand=True, padx=14, pady=22)
        self.container = self.frame(self.root)
        self.container.pack(side="left", fill="both", expand=True, padx=24, pady=22)
        self.header = self.frame(self.container)
        self.header.pack(fill="x", pady=(0, 22))
        self.status_dot = tk.Canvas(self.header, width=12, height=24, bd=0, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 9))
        self.state_label = self.label(self.header, text="Ready", font=("Segoe UI", 13, "bold"))
        self.state_label.pack(side="left")
        self.mode_var = tk.StringVar(value=app.settings.output_mode.title())
        self.mode_combo = ttk.Combobox(self.header, textvariable=self.mode_var, values=("Raw", "Polished"),
                                      state="readonly", style="Quiet.TCombobox", width=10)
        self.mode_combo.pack(side="left", padx=(16, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", lambda event: app._set_output_mode(self.mode_var.get().lower()))
        self.theme_button = self.button(self.header, command=app._toggle_theme, icon_name="moon", width=40, height=36)
        self.theme_button.pack(side="right", padx=(0, 4))
        self.main_page = self.frame(self.container)
        self.main_page.pack(fill="both", expand=True)
        self.error_label = self.label(self.main_page, textvariable=app.message, wraplength=560, justify="left")
        self.main_page.bind("<Configure>", lambda event: self.error_label.configure(wraplength=max(100, event.width - 10)))
        self.card = tk.Canvas(self.main_page, bd=0, highlightthickness=0)
        self.card.pack(fill="both", expand=True)
        self.text_frame = self.frame(self.card, surface=True)
        self.card_window = self.card.create_window(1, 1, window=self.text_frame, anchor="nw")
        self.card.bind("<Configure>", self._resize_card)
        self.preview = tk.Text(self.text_frame, wrap="word", font=("Segoe UI", 12), state="disabled",
            bd=0, highlightthickness=0, padx=22, pady=20, spacing1=3, spacing3=6,
            exportselection=False, cursor="xterm", relief="flat")
        self.preview.pack(fill="both", expand=True, padx=5, pady=5)
        self.placeholder = self.label(self.text_frame, text="Your text will appear here.", role="hint", surface=True, font=("Segoe UI", 12))
        self.placeholder.place(x=27, y=25)
        self.footer = self.frame(self.main_page)
        self.footer.pack(side="bottom", fill="x", before=self.card, pady=(16, 0))
        self.record_button = self.button(self.footer, text="Record", command=app._record_clicked, icon_name="mic", width=180, height=50, primary=True)
        self.record_button.pack()
        self.cancel_button = self.button(self.footer, text="Cancel", command=app.cancel, width=100, height=36)
        self.insert_raw_button = self.button(self.footer, text="Insert raw", command=app._insert_raw,
                                             width=180, height=40)
        self.insert_raw_button.tooltip_text = "Use the recovered raw text in the original input, without another API request"
        self.actions = self.frame(self.main_page)
        self.actions.pack(side="bottom", fill="x", after=self.footer, pady=(8, 0))
        self.retry_button = self.button(self.actions, text="Retry insertion", command=app._retry_insertion, width=150, height=34)
        self.latency_status = tk.StringVar()
        self.label(self.actions, textvariable=self.latency_status, role="muted", font=("Segoe UI", 8)).pack(side="right")
        self.settings_page = self.frame(self.container)
        self._build_settings()
        from .workspace import WorkspaceView
        self.workspace = WorkspaceView(self, self.sidebar_body)
        # The sidebar's own width is fixed (not auto-sized from children) so the
        # layout stays stable while sessions/transcripts are added/removed; grow
        # it once here to fit the widest actual button, so DPI scaling or longer
        # button text (e.g. "Transcribe file…") can't clip its own label.
        self.root.update_idletasks()
        needed = max((button.winfo_reqwidth() for button in self.workspace.sidebar_buttons), default=0)
        if needed + 28 > self.sidebar.winfo_reqwidth():
            self.sidebar.configure(width=needed + 28)
        self.root.bind("<Escape>", self._escape, add="+")
        self.apply_theme(app.settings.theme)

    def frame(self, parent, surface=False, **options):
        result = tk.Frame(parent, **options)
        self.frames.append((result, "surface" if surface else "bg"))
        return result

    def label(self, parent, role="text", surface=False, **options):
        result = tk.Label(parent, bd=0, anchor="w", **options)
        self.labels.append((result, role, "surface" if surface else "bg"))
        return result

    def button(self, parent, **options):
        result = RoundedButton(parent, **options)
        self.buttons.append(result)
        return result

    def scroll_area(self, parent, on_width=None):
        return ScrollableArea(self, parent, on_width=on_width)

    def _build_settings(self):
        app = self.app
        self.settings_wrapped = []
        self.settings_area = self.scroll_area(self.settings_page, on_width=self._resize_settings)
        self.settings_body = self.settings_area.body
        self.label(self.settings_body, text="Settings", font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(0, 18))
        self.hotkey_var = tk.StringVar(value=app.settings.hotkey)
        self.transcribe_var = tk.StringVar(value=app.settings.transcription_model)
        self.refine_var = tk.StringVar(value=app.settings.refinement_model)
        self.provider_var = tk.StringVar(value=PROVIDER_NAMES[app.settings.provider])
        self.overlay_position_var = tk.StringVar(value=app.settings.overlay_position.replace("-", " ").title())
        self.overlay_margin_var = tk.StringVar(value=str(app.settings.overlay_margin))
        self.backend_var = tk.StringVar(value="Local Whisper" if app.settings.transcription_backend == "local" else "Cloud API")
        self.refine_backend_var = tk.StringVar(value="Local (rule-based)" if app.settings.refinement_backend == "local"
                                               else "Local (LLM)" if app.settings.refinement_backend == "local_llm" else "Cloud")
        self.local_model_var = tk.StringVar(value=app.settings.local_model)
        self.tracking_var = tk.BooleanVar(value=app.settings.track_application)
        self.title_tracking_var = tk.BooleanVar(value=app.settings.collect_window_title)
        form = self.frame(self.settings_body)
        form.pack(fill="x")
        self.setting_widgets = []
        for label, variable in (("Transcription source", self.backend_var), ("Cloud provider · used when refinement is Cloud", self.provider_var), ("Global shortcut", self.hotkey_var),
                                ("Speech model · cloud transcription only, unused by Local Whisper", self.transcribe_var),
                                ("Refinement method · how Polished mode cleans up text", self.refine_backend_var),
                                ("Refinement model · only used when refinement method is Cloud", self.refine_var),
                                ("Local CPU model · optional", self.local_model_var),
                                ("Floating indicator position", self.overlay_position_var),
                                ("Indicator margin · logical pixels", self.overlay_margin_var)):
            self.label(form, text=label, role="muted", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
            if variable is self.backend_var:
                entry = ttk.Combobox(form, textvariable=variable, values=("Cloud API", "Local Whisper"), state="readonly", style="Quiet.TCombobox")
                entry.bind("<<ComboboxSelected>>", lambda event: self._update_cloud_field_state())
            elif variable is self.refine_backend_var:
                entry = ttk.Combobox(form, textvariable=variable, values=("Cloud", "Local (rule-based)", "Local (LLM)"), state="readonly", style="Quiet.TCombobox")
                entry.bind("<<ComboboxSelected>>", lambda event: self._update_cloud_field_state())
            elif variable is self.local_model_var:
                entry = ttk.Combobox(form, textvariable=variable, values=("tiny", "base", "small"), state="readonly", style="Quiet.TCombobox")
                entry.bind("<<ComboboxSelected>>", lambda event: self._refresh_model_status())
            elif variable is self.provider_var:
                entry = ttk.Combobox(form, textvariable=variable, values=tuple(PROVIDER_NAMES.values()), state="readonly", style="Quiet.TCombobox")
                self.provider_combo = entry
                entry.bind("<<ComboboxSelected>>", app._provider_changed)
            elif variable is self.overlay_position_var:
                entry = ttk.Combobox(form, textvariable=variable, values=("Bottom Center", "Bottom Left", "Bottom Right", "Top Center"),
                                     state="readonly", style="Quiet.TCombobox")
            else:
                entry = ttk.Entry(form, textvariable=variable, style="Quiet.TEntry")
            entry.pack(fill="x", pady=(0, 12), ipady=2)
            self.setting_widgets.append(entry)
            if variable is self.transcribe_var:
                self.speech_model_entry = entry
            elif variable is self.refine_var:
                self.refine_model_entry = entry
        self._update_cloud_field_state()
        for text, variable in (("Track destination application names in saved sessions", self.tracking_var),
                               ("Also collect window titles · may include private document names", self.title_tracking_var)):
            control = ttk.Checkbutton(form, text=text, variable=variable, style="Quiet.TCheckbutton")
            control.pack(anchor="w", pady=(0, 10))
            self.setting_widgets.append(control)
        self.local_status = tk.StringVar(value="Optional local models are never downloaded automatically. Raw local mode works without a cloud key.")
        whisper_row = self.frame(form)
        whisper_row.pack(anchor="w", fill="x", pady=(5, 8))
        download = self.button(whisper_row, text="Download local Whisper model", command=app._download_model, width=260, height=38)
        download.pack(side="left")
        self.setting_widgets.append(download)
        self.whisper_status_var = tk.StringVar()
        self.label(whisper_row, textvariable=self.whisper_status_var, role="muted", font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))
        llm_row = self.frame(form)
        llm_row.pack(anchor="w", fill="x", pady=(0, 10))
        download_refine = self.button(llm_row, text="Download refinement model (LLM)", command=app._download_refine_model, width=260, height=38)
        download_refine.pack(side="left")
        self.setting_widgets.append(download_refine)
        self.llm_status_var = tk.StringVar()
        self.label(llm_row, textvariable=self.llm_status_var, role="muted", font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))
        self.label(form, textvariable=self.local_status, role="muted", wraplength=555, justify="left", font=("Segoe UI", 9)).pack(fill="x", pady=(0, 15))
        self._refresh_model_status()
        actions = self.frame(self.settings_body)
        actions.pack(fill="x", pady=(3, 12))
        for text, callback, width in (("Set API key", app._set_key, 140), ("Check API", app._check_api, 132), ("Delete key", app._delete_key, 124)):
            control = self.button(actions, text=text, command=callback, width=width, height=38)
            control.pack(side="left", padx=(0, 5))
            self.setting_widgets.append(control)
        apply_button = self.button(self.settings_body, text="Apply settings", command=app._save_settings, width=190, height=44, primary=True)
        apply_button.pack(anchor="w", pady=(0, 16))
        self.setting_widgets.append(apply_button)
        self.key_status, self.api_url, self.privacy_message, self.tray_status = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        for variable in (self.key_status, self.api_url, self.tray_status, self.privacy_message, app.message):
            label = self.label(self.settings_body, textvariable=variable, role="muted", font=("Segoe UI", 9), wraplength=555, justify="left")
            label.pack(fill="x", pady=(0, 8))
            self.settings_wrapped.append(label)

    def _refresh_model_status(self):
        """File-presence only; never imports faster-whisper/llama-cpp-python."""
        from .local_stt import is_downloaded as whisper_downloaded
        from .local_llm import is_downloaded as llm_downloaded
        models_dir = self.app.store.directory / "models"
        model = self.local_model_var.get()
        self.whisper_status_var.set(f"{model}: downloaded" if whisper_downloaded(model, models_dir) else f"{model}: not downloaded")
        self.llm_status_var.set("downloaded" if llm_downloaded(models_dir) else "not downloaded")

    def _update_cloud_field_state(self):
        local_transcription = self.backend_var.get() == "Local Whisper"
        local_refinement = self.refine_backend_var.get() in ("Local (rule-based)", "Local (LLM)")
        busy = self.app.is_busy()
        self.speech_model_entry.configure(state="disabled" if local_transcription or busy else "normal")
        self.refine_model_entry.configure(state="disabled" if local_refinement or busy else "normal")
        # Cloud provider is only fully unused when neither stage touches the cloud.
        provider_unused = local_transcription and local_refinement
        self.provider_combo.configure(state="disabled" if busy or provider_unused else "readonly")

    def show_file_transcript(self, source_name, segments):
        # Plain tk widgets, built directly (not via self.label/frame/button): this
        # transient window must not be registered in the app-lifetime theme-redraw
        # lists, or closing it would leave a destroyed widget there, crashing the
        # next theme toggle.
        import os
        from tkinter import filedialog
        p = self.palette

        def fmt(seconds):
            total = int(seconds)
            return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}.{int((seconds - total) * 1000):03d}"

        lines = [f"[{fmt(row['start'])} – {fmt(row['end'])}]  {row['text']}" for row in segments]
        full_text = "\n".join(lines)

        window = tk.Toplevel(self.root)
        window.title("Transcript — " + source_name)
        window.geometry("640x520")
        window.configure(bg=p["bg"])
        window.transient(self.root)

        tk.Label(window, text=source_name, bg=p["bg"], fg=p["text"], anchor="w",
                font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(window, text=f"{len(segments)} segment(s) · transcribed locally; the file was never uploaded.",
                bg=p["bg"], fg=p["muted"], anchor="w", font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(0, 10))

        body = tk.Frame(window, bg=p["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        text_widget = tk.Text(body, wrap="word", font=("Consolas", 10), bd=0, highlightthickness=0,
                              bg=p["surface"], fg=p["text"], insertbackground=p["text"],
                              selectbackground=p["selection"], selectforeground=p["text"])
        scrollbar = tk.Scrollbar(body, orient="vertical", command=text_widget.yview)
        text_widget.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text_widget.pack(side="left", fill="both", expand=True)
        text_widget.insert("1.0", full_text)
        text_widget.configure(state="disabled")

        def save_as_text():
            target = filedialog.asksaveasfilename(title="Save transcript as text", defaultextension=".txt",
                filetypes=[("Text", "*.txt")], parent=window,
                initialfile=os.path.splitext(source_name)[0] + "-transcript.txt")
            if target:
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(full_text + "\n")

        def copy_all():
            window.clipboard_clear()
            window.clipboard_append(full_text)

        actions = tk.Frame(window, bg=p["bg"])
        actions.pack(fill="x", padx=16, pady=(0, 14))
        for text, command in (("Copy all", copy_all), ("Save as text", save_as_text), ("Close", window.destroy)):
            tk.Button(actions, text=text, command=command, bg=p["surface"], fg=p["text"],
                     activebackground=p["hover"], relief="flat", padx=12, pady=6).pack(side="left", padx=(0, 8))

        window.update_idletasks()
        try:
            set_titlebar_theme(window.winfo_id(), self.app.settings.theme == "dark")
        except Exception:
            pass

    def _resize_settings(self, width):
        for label in self.settings_wrapped:
            label.configure(wraplength=max(100, width - 8))

    def toggle_settings(self):
        if self.page == "main":
            self.main_page.pack_forget()
            self.settings_page.pack(fill="both", expand=True)
            self.page = "settings"
            self._refresh_model_status()
        else:
            self.settings_page.pack_forget()
            self.main_page.pack(fill="both", expand=True)
            self.page = "main"

    def _escape(self, event):
        if self.page == "settings":
            self.toggle_settings()
            return "break"

    def _resize_card(self, event=None):
        width, height = self.card.winfo_width(), self.card.winfo_height()
        if width < 4 or height < 4:
            return
        self.card.delete("border")
        rounded(self.card, 1, 1, width - 1, height - 1, 12, fill=self.palette["surface"], outline=self.palette["border"], width=1, tags="border")
        self.card.tag_lower("border")
        self.card.coords(self.card_window, 5, 5)
        self.card.itemconfigure(self.card_window, width=width - 10, height=height - 10)

    def state_changed(self, state, busy):
        color = self.palette["error"] if state in (State.RECORDING, State.ERROR) else self.palette["ready"] if state == State.IDLE else self.palette["processing"]
        self.state_label.configure(fg=self.palette["text"])
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 8, 10, 16, fill=color, outline="")
        self.error_label.configure(fg=self.palette["error"] if state == State.ERROR else self.palette["muted"])
        if state in (State.ERROR, State.CANCELLING):
            self.error_label.pack(fill="x", before=self.card, pady=(0, 15))
        else:
            self.error_label.pack_forget()
        if state == State.RECORDING:
            self.record_button.configure(text="Stop", icon_name="stop", state="normal")
        elif busy:
            self.record_button.configure(text="Processing", icon_name=None, state="disabled")
        else:
            self.record_button.configure(text="Record", icon_name="mic", state="normal")
        self.record_button.tooltip_text = f"Record / stop · {self.app.settings.hotkey}"
        if busy and not self.app.closing:
            self.cancel_button.pack(pady=(5, 0))
        else:
            self.cancel_button.pack_forget()
        self.cancel_button.configure(state="disabled" if self.app.closing else "normal")
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        self.mode_var.set(self.app.settings.output_mode.title())
        recovery = hasattr(self, "workspace") and self.workspace.recovery_visible()
        if self.app.pending_raw and self.app.pending_raw.can_retry and recovery and not busy:
            self.insert_raw_button.pack(before=self.record_button, pady=(0, 8))
        else:
            self.insert_raw_button.pack_forget()
        result = self.app.last_result
        if result and result.can_retry and result.insertion_error and recovery and not busy:
            self.retry_button.pack(side="left", padx=(5, 0))
        else:
            self.retry_button.pack_forget()
        if hasattr(self, "workspace"):
            self.workspace.state_changed(busy)

    def preview_changed(self, text):
        if text:
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=27, y=25)

    def apply_theme(self, theme):
        self.palette = PALETTES[theme]
        p = self.palette
        self.root.configure(bg=p["bg"])
        for widget, role in self.frames:
            widget.configure(bg=p[role])
        for widget, role, background in self.labels:
            widget.configure(bg=p[background], fg=p[role])
        for widget in self.buttons:
            widget.apply_theme(p)
        self.card.configure(bg=p["bg"])
        self.status_dot.configure(bg=p["bg"])
        self.settings_area.canvas.configure(bg=p["bg"])
        self.preview.configure(bg=p["surface"], fg=p["text"], insertbackground=p["text"],
                               selectbackground=p["selection"], selectforeground=p["text"])
        self.style.configure("Quiet.Vertical.TScrollbar", background=p["border"], troughcolor=p["bg"],
                             bordercolor=p["bg"], arrowcolor=p["muted"], width=10)
        self.style.configure("Quiet.TEntry", fieldbackground=p["surface"], foreground=p["text"],
                             bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"], padding=7)
        self.style.configure("Quiet.TCombobox", fieldbackground=p["surface"], background=p["surface"],
                             foreground=p["text"], arrowcolor=p["muted"], bordercolor=p["border"],
                             lightcolor=p["border"], darkcolor=p["border"], padding=7)
        self.style.map("Quiet.TCombobox", fieldbackground=[("readonly", p["surface"]), ("disabled", p["bg"])],
                       foreground=[("readonly", p["text"]), ("disabled", p["muted"])])
        self.root.option_add("*TCombobox*Listbox.background", p["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["selection"])
        self.style.configure("Quiet.TCheckbutton", background=p["bg"], foreground=p["text"], font=("Segoe UI", 9))
        self.style.map("Quiet.TCheckbutton", background=[("active", p["bg"])], foreground=[("disabled", p["muted"])])
        if hasattr(self, "workspace"):
            self.workspace.apply_theme(p)
        self.theme_button.configure(icon_name="moon" if theme == "light" else "sun")
        self.theme_button.tooltip_text = "Switch to dark theme" if theme == "light" else "Switch to light theme"
        self._resize_card()
        self.state_changed(self.app.state, self.app.is_busy())
        self.root.update_idletasks()
        set_titlebar_theme(self.root.winfo_id(), theme == "dark")

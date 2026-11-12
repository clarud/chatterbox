# Workspace and floating indicator — version 0.4

The workspace uses neutral light/dark palettes, Segoe UI typography, subtle borders, rounded controls and a sidebar for New Session, Recent Sessions, Quick Dictation and Settings. No redundant application-brand header or technical subtitle is displayed. The main panel shows the current context, a chronological session timeline when selected, reviewable text and always-accessible Record/Copy controls. Settings are a separate scrollable page. Appearance and Raw/Polished selection persist.

The workspace is optional. Closing hides it to the tray when available; the global shortcut and recording indicator continue independently. `--background` starts hidden, with a taskbar fallback if notification-area support fails. Tray actions open workspace/settings, switch modes and exit. No auto-start registration is created.

## Recording feedback

A native Win32 layered/tool/topmost/no-activate window forms the floating pill. It uses `WS_EX_NOACTIVATE` and transparent hit testing, `SW_SHOWNOACTIVATE`, non-activating placement flags and a rounded window region. It does not call SetForegroundWindow or activate/deiconify a Tk Toplevel. MonitorFromWindow selects the original target's monitor; GetMonitorInfo work-area bounds keep it above the taskbar. Scoped per-monitor DPI awareness and monitor scale factor control pixel geometry without changing the working Tk process's global DPI assumptions.[1] [2]

Recording starts only after the existing waveIn recorder confirms active capture and its first returned PCM buffer. A reused short-buffer ring supplies a thread-safe snapshot of captured seconds and RMS amplitude. No second microphone stream is opened. The indicator polls that snapshot at 40 ms intervals—approximately 25 Hz—and displays a 13-bar amplitude history. A small fixed noise floor, proportional gain, time-based attack/release smoothing and a silence reset avoid jitter and fabricated idle motion. Processing dots are distinct from the actual-input waveform.

The pill shows Recording, Transcribing, Refining, Success and Error, plus output mode and captured duration. Success hides after about 1.3 seconds; errors remain for about 8 seconds. Idle/cancelled states hide. Position is configurable as bottom center/left/right or top center, with an 8–160 logical-pixel work-area margin. Recording/capture errors abort without uploading audio.

## Focus and input safeguards

The original target is captured before microphone startup. Native handle/process/thread/class/focus and available caret metadata are checked before insertion; the overlay never replaces the target. The Record button requires a recently observed, valid external input within 30 seconds and fails instead of guessing. The hotkey directly in the intended field is the simplest and best-tested background workflow. Unavailable capture status does not show a false Recording state.

Clipboard ownership markers supplement sequence checks. Text is preserved after API/insertion errors, and possibly-sent receipts suppress duplicate retry. Read-only history review and explicit Copy do not silently reinsert edited/history text. Session selection/provider/mode changes are blocked during active operations; cosmetic theme changes remain allowed.

## Validation and framework limits

Actual workspace light/dark, Quick Dictation, Settings and native pill screenshots were captured from owned windows using isolated synthetic text/RMS. A concrete footer-clipping issue found during review was fixed: the bottom action controls reserve space before the growing timeline/text area. Sidebar icon/text spacing was corrected. Previews are not real microphone sessions or production latency data.

Tk remains the working frontend; see [framework decision](framework.md). Mixed-DPI main-window behavior, comprehensive accessibility/high-contrast support, and pixel-identical Notion/ChatGPT styling are not claimed. Native dialogs retain platform styling. The overlay's real single-monitor native test passed, but mixed-monitor hardware configurations still need acceptance testing. The indicator is intentionally click-through: stop/cancel through the shortcut, workspace or tray Exit, not the pill.

## References

[1]: https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features "Microsoft window styles and layered window behavior"
[2]: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setthreaddpiawarenesscontext "Microsoft scoped thread DPI awareness"
[3]: https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveinaddbuffer "Microsoft audio input buffer lifecycle"

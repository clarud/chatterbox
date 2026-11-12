# Framework decision

Keep the current Tk/Win32 implementation. It runs on a standard Windows Python 3.12+ install with Tk, with zero third-party packages required for cloud dictation. (Development/testing for this decision happened against an MSYS UCRT Python build rather than standard Windows CPython wheel tags; that's a development-environment detail, not a runtime requirement — end users should use official Windows Python.) PySide6 was not installed or tested here.

PySide6 is a reasonable future frontend, especially for richer timeline cards, accessibility, animation and consistent per-monitor scaling. Qt provides both Widgets and declarative Qt Quick. Official installation guidance recommends official Python and a virtual environment; its wheels include Qt binaries. That adds runtime/distribution dependencies and requires testing a different environment, rather than simply restyling the already-working UI.[1]

Qt 6 also defaults to Per-Monitor DPI Aware V2 on Windows and handles device-independent geometry and display scale changes. That is an advantage over the current Tk workspace, whose mixed-DPI behavior can depend on Windows bitmap scaling. The floating recording indicator already uses native per-monitor positioning without changing the main window's process-wide DPI assumptions.[2]

A framework migration is therefore not justified before qualifying live dictation and insertion reliability. The engine remains modular: audio, API/local provider, pipeline, native focus/clipboard and SQLite storage do not depend on Tk. Only the controller/presentation layers and native indicator scheduling would need replacement. No Qt installation, dependency download, or migration was performed.

## References

[1]: https://doc.qt.io/qtforpython-6.10/gettingstarted.html "Qt for Python getting started"
[2]: https://doc.qt.io/qtforpython-6/overviews/qtdoc-highdpi.html "Qt high-DPI support"

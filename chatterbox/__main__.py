"""Run with python -m chatterbox. Diagnostics never record audio."""
import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description="Windows global-hotkey voice dictation")
    parser.add_argument("--diagnose", action="store_true", help="query audio capabilities and runtime; do not record")
    parser.add_argument("--smoke-ui", action="store_true", help="open UI for one second, then exit without recording")
    parser.add_argument("--check-api", action="store_true", help="check the stored key and configured model listing; no audio")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray; fall back to minimized if tray unavailable")
    args = parser.parse_args()
    try:
        if args.diagnose:
            from .audio import diagnostics
            from .settings import Store
            from .tls import create_context
            import tkinter
            store = Store()
            settings = store.load()
            print(json.dumps({"python": sys.version.split()[0], "executable": sys.executable,
                              "tk": tkinter.TkVersion, "audio": diagnostics(),
                              "tls_trusted_ca_count": create_context().cert_store_stats()["x509_ca"],
                              "provider": settings.provider, "speech_model": settings.transcription_model,
                              "refinement_model": settings.refinement_model,
                              "transcription_backend": settings.transcription_backend, "local_model": settings.local_model,
                              "output_mode": settings.output_mode, "track_application": settings.track_application,
                              "collect_window_title": settings.collect_window_title,
                              "settings_directory": str(store.directory),
                              "api_key_stored": store.key_path_for(settings.provider).exists()}, indent=2))
        elif args.check_api:
            from .api import create_service
            from .settings import Store
            store = Store()
            print(create_service(store, store.load()).check_models())
        else:
            from .app import run
            run(smoke=args.smoke_ui, background=args.background)
    except Exception as exc:
        from .crash_log import log_crash
        log_crash(exc)
        try:
            print("Chatterbox: " + str(exc), file=sys.stderr)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

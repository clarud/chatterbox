import json
import tempfile
import unittest

from chatterbox.settings import Settings, Store


class ThemeSettingsTests(unittest.TestCase):
    def test_theme_roundtrip_preserves_provider_models_and_hotkey(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            settings = Settings.for_provider("openrouter", "Ctrl+I")
            settings.theme = "dark"
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertFalse(store.key_path_for("openrouter").exists())

    def test_pre_theme_settings_default_to_light_without_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            legacy = {"hotkey": "Ctrl+I", "transcription_model": "openai/gpt-transcribe",
                      "refinement_model": "openai/gpt-5.6-terra", "provider": "openrouter"}
            original = json.dumps(legacy)
            store.config_path.write_text(original)
            self.assertEqual(store.load().theme, "light")
            self.assertEqual(store.load().provider, "openrouter")
            self.assertEqual(store.config_path.read_text(), original)

    def test_invalid_theme_is_rejected(self):
        settings = Settings(theme="invalid")
        with self.assertRaisesRegex(ValueError, "Theme"):
            settings.validate()


if __name__ == "__main__":
    unittest.main()

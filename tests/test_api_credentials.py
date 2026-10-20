# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Exercise real QGIS encryption without touching the user's authentication DB."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class ApiCredentialStoreTests(unittest.TestCase):
    def test_encrypted_key_lifecycle_in_isolated_qgis_profile(self):
        script = textwrap.dedent("""
            import os
            from pathlib import Path
            from qgis.PyQt.QtCore import QSettings
            from qgis.core import QgsApplication, QgsAuthMethodConfig
            from acalc.api_connections import ApiConnectionStore, QgisCredentialStore, SETTINGS_KEY
            from acalc.schema import ModelError

            root = Path(os.environ["QGIS_AUTH_DB_DIR_PATH"])
            app = QgsApplication([], False, str(root / "profile"))
            app.initQgis()
            try:
                manager = app.authManager()
                database = Path(app.qgisAuthDatabaseFilePath()).resolve()
                assert database.is_relative_to(root.resolve()), database
                assert manager.setMasterPassword("temporary-acalc-test-master-password", True)
                credentials = QgisCredentialStore(manager)
                settings = QSettings(str(root / "connections.ini"), QSettings.IniFormat)
                store = ApiConnectionStore(settings, credentials)
                first_key = "synthetic-acalc-test-key-first"
                replacement_key = "synthetic-acalc-test-key-replacement"

                connection = store.save("Private engine", "https://engine.example.test:8343",
                                        mode="remote", client_key=first_key)
                authcfg = connection["authcfg"]
                manager.clearAllCachedConfigs()
                assert credentials.load(authcfg) == first_key
                public = QgsAuthMethodConfig()
                ok, public = manager.loadAuthenticationConfig(authcfg, public, False)
                assert ok and not public.config("password")

                connection = store.save("Private engine", connection["base_url"], connection["id"],
                                        mode="remote", client_key=replacement_key)
                assert connection["authcfg"] == authcfg
                manager.clearAllCachedConfigs()
                assert credentials.load(authcfg) == replacement_key
                settings.sync()
                saved_settings = settings.value(SETTINGS_KEY)
                assert authcfg in saved_settings
                for secret in (first_key, replacement_key):
                    assert secret not in saved_settings
                    for path in root.rglob("*"):
                        if path.is_file():
                            assert secret.encode() not in path.read_bytes(), path

                store.remove(connection["id"])
                assert not store.connections()
                try:
                    credentials.load(authcfg)
                except ModelError:
                    pass
                else:
                    raise AssertionError("Removing a connection retained its saved key")
            finally:
                app.exitQgis()
        """)
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QGIS_AUTH_DB_DIR_PATH=directory)
            # An inherited URI or password file must never redirect this child
            # to a real database or supply a real master password.
            environment.pop("QGIS_AUTH_DB_URI", None)
            environment.pop("QGIS_AUTH_PASSWORD_FILE", None)
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()

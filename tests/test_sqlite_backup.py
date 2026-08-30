import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import Mock, patch

from backend.sqlite_backup import SQLiteBackupError, _publish_without_overwrite, create_backup, main, restore_drill, verify_backup


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_directory = self.root / "source"
        self.destination = self.root / "backups"
        self.source_directory.mkdir()
        self.destination.mkdir()
        self.source = self.source_directory / "orders.sqlite3"
        connection = sqlite3.connect(self.source)
        connection.execute("CREATE TABLE custom_song_orders (id INTEGER PRIMARY KEY, brief_json TEXT NOT NULL)")
        connection.execute("INSERT INTO custom_song_orders (brief_json) VALUES (?)", ('{"story":"private brief text"}',))
        connection.execute("INSERT INTO custom_song_orders (brief_json) VALUES (?)", ('{"story":"second private brief"}',))
        connection.commit()
        connection.close()
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_successful_backup_is_validated_and_manifest_is_safe(self):
        backup, manifest_path = create_backup(
            source_db_path=self.source,
            destination_directory=self.destination,
            environment="sandbox",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(backup.is_file())
        self.assertEqual(self.source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(manifest["environment"], "sandbox")
        self.assertEqual(manifest["custom_song_orders_count"], 2)
        self.assertEqual(manifest["sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        self.assertNotIn("private brief text", manifest_path.read_text(encoding="utf-8"))
        copied = sqlite3.connect(backup)
        try:
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copied.execute("SELECT COUNT(*) FROM custom_song_orders").fetchone()[0], 2)
        finally:
            copied.close()

    def test_rejects_live_missing_source_and_dangerous_destination(self):
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=self.destination, environment="live")
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.root / "missing.sqlite3", destination_directory=self.destination, environment="sandbox")
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=self.source_directory, environment="sandbox")

    def test_rejects_overwrite(self):
        fixed_uuid = "12345678-1234-5678-1234-567812345678"
        fixed_time = "2026-08-30T21:00:00Z"
        with patch("backend.sqlite_backup.uuid.uuid4", return_value=fixed_uuid), patch(
            "backend.sqlite_backup.datetime"
        ) as clock:
            clock.now.return_value.isoformat.return_value = fixed_time.replace("Z", "+00:00")
            create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
            with self.assertRaises(SQLiteBackupError):
                create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")

    def test_validation_failure_publishes_no_final_artifacts(self):
        with patch("backend.sqlite_backup._validate_backup", side_effect=SQLiteBackupError("validation failed")):
            with self.assertRaises(SQLiteBackupError):
                create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_source_uri_is_read_only_and_escapes_portable_special_paths(self):
        special_directory = self.root / "source with spaces & #"
        special_directory.mkdir()
        special_source = special_directory / "orders.sqlite3"
        special_source.write_bytes(self.source.read_bytes())
        uri = special_source.as_uri()
        self.assertIn("%20", uri)
        self.assertIn("%26", uri)
        self.assertIn("%23", uri)
        readonly = sqlite3.connect(uri + "?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("INSERT INTO custom_song_orders (brief_json) VALUES ('no write')")
        finally:
            readonly.close()
        backup, _ = create_backup(source_db_path=special_source, destination_directory=self.destination, environment="sandbox")
        self.assertTrue(backup.exists())
        windows_uri = PureWindowsPath(r"C:\folder with spaces\orders#?.sqlite3").as_uri()
        self.assertEqual(windows_uri, "file:///C:/folder%20with%20spaces/orders%23%3F.sqlite3")

    def test_collision_after_precheck_preserves_backup_winner_and_cleans_up(self):
        winner = None

        def race(temporary, final):
            nonlocal winner
            winner = final
            final.write_bytes(b"winner")
            return _publish_without_overwrite(temporary, final)

        with patch("backend.sqlite_backup._publish_without_overwrite", side_effect=race):
            with self.assertRaises(SQLiteBackupError):
                create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        assert winner is not None
        self.assertEqual(winner.read_bytes(), b"winner")
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()), [winner.name])

    def test_manifest_collision_removes_only_our_backup_and_preserves_winner(self):
        winner = None
        calls = 0

        def race(temporary, final):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _publish_without_overwrite(temporary, final)
            nonlocal winner
            winner = final
            final.write_bytes(b"manifest winner")
            return _publish_without_overwrite(temporary, final)

        with patch("backend.sqlite_backup._publish_without_overwrite", side_effect=race):
            with self.assertRaises(SQLiteBackupError):
                create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        assert winner is not None
        self.assertEqual(winner.read_bytes(), b"manifest winner")
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()), [winner.name])

    def test_failure_paths_leave_no_final_or_temporary_artifacts(self):
        source_connection = sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)
        failing_source = Mock()
        failing_source.backup.side_effect = sqlite3.Error("backup failure")
        failing_source.close.side_effect = source_connection.close
        real_connect = sqlite3.connect

        def fail_backup_connect(path, *args, **kwargs):
            if kwargs.get("uri"):
                return failing_source
            return real_connect(path, *args, **kwargs)

        for injected in (
            patch("backend.sqlite_backup.sqlite3.connect", side_effect=fail_backup_connect),
            patch("backend.sqlite_backup.Path.write_text", side_effect=OSError("manifest failure")),
            patch("backend.sqlite_backup._publish_without_overwrite", side_effect=OSError("publish failure")),
        ):
            with self.subTest(injected=injected):
                with injected, self.assertRaises((SQLiteBackupError, OSError)):
                    create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
                self.assertEqual(list(self.destination.iterdir()), [])
        # The backup must be removed if only the subsequent manifest publication fails.
        publish_calls = 0

        def fail_manifest_publish(temporary, final):
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 1:
                return _publish_without_overwrite(temporary, final)
            raise OSError("manifest publish failure")

        with patch("backend.sqlite_backup._publish_without_overwrite", side_effect=fail_manifest_publish):
            with self.assertRaises(SQLiteBackupError):
                create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_cli_requires_environment_and_rejects_live(self):
        with self.assertRaises(SystemExit):
            main(["create", "--source-db", str(self.source), "--destination-directory", str(self.destination)])
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["create", "--source-db", str(self.source), "--destination-directory", str(self.destination), "--environment", "live"],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 1)
        self.assertIn("only environment=sandbox", stderr.getvalue().lower())

    def test_verify_succeeds_without_modifying_backup_and_rejects_mismatches(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        original = backup.read_bytes()
        self.assertEqual(verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")["custom_song_orders_count"], 2)
        self.assertEqual(backup.read_bytes(), original)
        data = json.loads(manifest.read_text())
        data["environment"] = "live"
        manifest.write_text(json.dumps(data))
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")

    def test_verify_rejects_checksum_corruption_missing_table_and_bad_count(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        backup.write_bytes(b"corrupt")
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")
        replacement = self.destination / "empty.sqlite3"
        sqlite3.connect(replacement).close()
        data = json.loads(manifest.read_text())
        data.update({"backup_filename": replacement.name, "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(), "backup_size_bytes": replacement.stat().st_size})
        manifest.write_text(json.dumps(data))
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=replacement, manifest_path=manifest, environment="sandbox")

    def test_restore_drill_is_new_isolated_and_preserves_backup(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        original = backup.read_bytes()
        destination = self.root / "drill with spaces" / "restored.sqlite3"
        destination.parent.mkdir()
        result = restore_drill(backup_path=backup, manifest_path=manifest, destination_db=destination, environment="sandbox")
        self.assertEqual(result, destination.resolve())
        self.assertEqual(backup.read_bytes(), original)
        restored = sqlite3.connect(result)
        try:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM custom_song_orders").fetchone()[0], 2)
        finally:
            restored.close()
        with self.assertRaises(SQLiteBackupError):
            restore_drill(backup_path=backup, manifest_path=manifest, destination_db=destination, environment="sandbox")

    def test_verify_rejects_invalid_incomplete_and_wrong_type_manifests_safely(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        original_backup = backup.read_bytes()
        for content in ("{not json", json.dumps({"environment": "sandbox"}), json.dumps({
            "environment": "sandbox", "sha256": hashlib.sha256(original_backup).hexdigest(),
            "backup_filename": backup.name, "backup_size_bytes": "wrong", "custom_song_orders_count": "2", "pragma_user_version": "0",
        })):
            with self.subTest(content=content):
                manifest.write_text(content)
                with self.assertRaises(SQLiteBackupError):
                    verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")
                self.assertEqual(backup.read_bytes(), original_backup)
                self.assertEqual(sorted(path.name for path in self.destination.iterdir()), sorted([backup.name, manifest.name]))

    def test_verify_rejects_manifest_count_and_user_version_mismatches(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        data = json.loads(manifest.read_text())
        for field, value in (("custom_song_orders_count", 99), ("pragma_user_version", 7)):
            with self.subTest(field=field):
                changed = dict(data)
                changed[field] = value
                manifest.write_text(json.dumps(changed))
                with self.assertRaises(SQLiteBackupError):
                    verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")

    def test_verify_detects_sqlite_corruption_even_with_updated_checksum(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        backup.write_bytes(b"not a sqlite database")
        data = json.loads(manifest.read_text())
        data["sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
        data["backup_size_bytes"] = backup.stat().st_size
        manifest.write_text(json.dumps(data))
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")

    def test_restore_drill_failure_and_destination_race_leave_no_partial_file(self):
        backup, manifest = create_backup(source_db_path=self.source, destination_directory=self.destination, environment="sandbox")
        original = backup.read_bytes()
        destination = self.root / "drill.sqlite3"
        real_validate = __import__("backend.sqlite_backup", fromlist=["_validate_backup"])._validate_backup
        with patch("backend.sqlite_backup._validate_backup", side_effect=[real_validate(backup), SQLiteBackupError("drill validation failure")]):
            with self.assertRaises(SQLiteBackupError):
                restore_drill(backup_path=backup, manifest_path=manifest, destination_db=destination, environment="sandbox")
        self.assertFalse(destination.exists())
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(any("restore-drill" in path.name for path in destination.parent.iterdir()))

        def winner(temporary, final):
            final.write_bytes(b"winner")
            return _publish_without_overwrite(temporary, final)

        with patch("backend.sqlite_backup._publish_without_overwrite", side_effect=winner):
            with self.assertRaises(SQLiteBackupError):
                restore_drill(backup_path=backup, manifest_path=manifest, destination_db=destination, environment="sandbox")
        self.assertEqual(destination.read_bytes(), b"winner")
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(any("restore-drill" in path.name for path in destination.parent.iterdir()))

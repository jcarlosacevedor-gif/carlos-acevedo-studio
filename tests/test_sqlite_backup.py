import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import Mock, patch

import backend.sqlite_backup as backup_module
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

    def test_wal_backup_is_consistent_with_an_active_writer_transaction(self):
        connection = sqlite3.connect(self.source)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        finally:
            connection.close()

        writer_ready = threading.Event()
        release_writer = threading.Event()
        writer_errors = []

        def write_uncommitted_row():
            try:
                writer = sqlite3.connect(self.source)
                try:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        "INSERT INTO custom_song_orders (brief_json) VALUES (?)",
                        ('{"story":"concurrent private brief"}',),
                    )
                    writer_ready.set()
                    if not release_writer.wait(timeout=10):
                        raise RuntimeError("writer release timed out")
                    writer.commit()
                finally:
                    writer.close()
            except Exception as error:  # surfaced in the main test thread below
                writer_errors.append(error)
                writer_ready.set()

        writer_thread = threading.Thread(target=write_uncommitted_row)
        writer_thread.start()
        self.assertTrue(writer_ready.wait(timeout=10))
        try:
            backup, manifest = create_backup(
                source_db_path=self.source,
                destination_directory=self.destination,
                environment="sandbox",
            )
        finally:
            release_writer.set()
            writer_thread.join(timeout=10)
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(writer_errors, [])
        details = verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")
        self.assertEqual(details["custom_song_orders_count"], 2)
        live = sqlite3.connect(self.source)
        try:
            self.assertEqual(live.execute("SELECT COUNT(*) FROM custom_song_orders").fetchone()[0], 3)
        finally:
            live.close()


class SQLitePreviewBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_root = self.root / "var" / "data"
        self.backup_root = self.data_root / "backups"
        self.restore_root = self.root / "tmp" / "carlos-acevedo-restore-drill"
        self.drill_directory = self.restore_root / "run-001"
        for directory in (self.backup_root, self.restore_root, self.drill_directory):
            directory.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                directory.chmod(0o700)
        self.source = self.data_root / "orders.sqlite3"
        connection = sqlite3.connect(self.source)
        connection.execute("CREATE TABLE custom_song_orders (id INTEGER PRIMARY KEY, brief_json TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO custom_song_orders (brief_json) VALUES (?)",
            ('{"story":"PRIVATE-BRIEF-SENTINEL","order":"ORDER-ID-SENTINEL"}',),
        )
        connection.commit()
        connection.close()
        if os.name == "posix":
            self.source.chmod(0o600)
        self.policy = patch.multiple(
            backup_module,
            PREVIEW_SOURCE_DB=self.source,
            PREVIEW_BACKUP_ROOT=self.backup_root,
            PREVIEW_RESTORE_ROOT=self.restore_root,
        )
        self.policy.start()

    def tearDown(self):
        self.policy.stop()
        self.temporary_directory.cleanup()

    def _create(self):
        return create_backup(
            source_db_path=self.source,
            destination_directory=self.backup_root,
            environment="preview",
        )

    def test_preview_create_verify_restore_and_private_outputs(self):
        backup, manifest_path = self._create()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["environment"], "preview")
        self.assertEqual(manifest["backup_filename"], backup.name)
        self.assertEqual(manifest["backup_size_bytes"], backup.stat().st_size)
        self.assertEqual(manifest["custom_song_orders_count"], 1)
        details = verify_backup(backup_path=backup, manifest_path=manifest_path, environment="preview")
        self.assertEqual(details["custom_song_orders_count"], 1)
        restored = restore_drill(
            backup_path=backup,
            manifest_path=manifest_path,
            destination_db=self.drill_directory / "restored.sqlite3",
            environment="preview",
        )
        self.assertTrue(restored.is_file())
        stdout, stderr = io.StringIO(), io.StringIO()
        self.assertEqual(main(["verify", "--backup-db", str(backup), "--manifest", str(manifest_path), "--environment", "preview"], stdout=stdout, stderr=stderr), 0)
        combined = stdout.getvalue() + stderr.getvalue() + manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE-BRIEF-SENTINEL", combined)
        self.assertNotIn("ORDER-ID-SENTINEL", combined)

    def test_preview_uses_online_backup_helper(self):
        with patch("backend.sqlite_backup._sqlite_online_backup", wraps=backup_module._sqlite_online_backup) as online_backup:
            self._create()
        online_backup.assert_called_once()

    def test_sandbox_cannot_label_preview_source(self):
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=self.backup_root, environment="sandbox")

    def test_preview_rejects_different_source_and_parent_traversal(self):
        other = self.data_root / "other.sqlite3"
        sqlite3.connect(other).close()
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=other, destination_directory=self.backup_root, environment="preview")
        outside_destination = self.root / "outside-backups"
        outside_destination.mkdir()
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=outside_destination, environment="preview")
        nested = self.data_root / "nested"
        nested.mkdir()
        traversed_source = nested / ".." / self.source.name
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=traversed_source, destination_directory=self.backup_root, environment="preview")
        backup_nested = self.backup_root / "nested"
        backup_nested.mkdir()
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=backup_nested / "..", environment="preview")

    def test_preview_rejects_source_and_directory_symlinks(self):
        real_source = self.data_root / "real.sqlite3"
        sqlite3.connect(real_source).close()
        source_link = self.data_root / "source-link.sqlite3"
        backup_link = self.data_root / "backup-link"
        try:
            source_link.symlink_to(real_source)
            backup_link.symlink_to(self.backup_root, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with patch.object(backup_module, "PREVIEW_SOURCE_DB", source_link):
            with self.assertRaises(SQLiteBackupError):
                create_backup(source_db_path=source_link, destination_directory=self.backup_root, environment="preview")
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=backup_link, environment="preview")

    def test_preview_verify_rejects_outside_traversal_symlink_and_wrong_environment(self):
        backup, manifest = self._create()
        outside = self.root / "outside.sqlite3"
        outside.write_bytes(b"not a backup")
        outside_manifest = self.root / "outside.json"
        outside_manifest.write_text("{}", encoding="utf-8")
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=outside, manifest_path=outside_manifest, environment="preview")
        nested = self.backup_root / "nested"
        nested.mkdir()
        if os.name == "posix":
            nested.chmod(0o700)
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=nested / ".." / backup.name, manifest_path=manifest, environment="preview")
        link = self.backup_root / "backup-link.sqlite3"
        try:
            link.symlink_to(backup)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(SQLiteBackupError):
                verify_backup(backup_path=link, manifest_path=manifest, environment="preview")
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=backup, manifest_path=manifest, environment="sandbox")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["environment"] = "sandbox"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=backup, manifest_path=manifest, environment="preview")

    def test_preview_create_never_overwrites_a_collision(self):
        fixed_uuid = "12345678-1234-5678-1234-567812345678"
        fixed_time = "2026-10-11T16:00:00Z"
        with patch("backend.sqlite_backup.uuid.uuid4", return_value=fixed_uuid), patch(
            "backend.sqlite_backup.datetime"
        ) as clock:
            clock.now.return_value.isoformat.return_value = fixed_time.replace("Z", "+00:00")
            backup, manifest = self._create()
            original_backup = backup.read_bytes()
            original_manifest = manifest.read_bytes()
            with self.assertRaises(SQLiteBackupError):
                self._create()
        self.assertEqual(backup.read_bytes(), original_backup)
        self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_preview_rejects_hardlink_aliases_to_live(self):
        backup_alias = self.backup_root / "live-alias.sqlite3"
        restore_alias = self.drill_directory / "live-alias.sqlite3"
        try:
            os.link(self.source, backup_alias)
            os.link(self.source, restore_alias)
        except OSError as error:
            self.skipTest(f"hardlinks unavailable: {error}")
        alias_manifest = backup_alias.with_suffix(".json")
        alias_manifest.write_text("{}", encoding="utf-8")
        if os.name == "posix":
            alias_manifest.chmod(0o600)
        with self.assertRaises(SQLiteBackupError):
            verify_backup(backup_path=backup_alias, manifest_path=alias_manifest, environment="preview")
        backup, manifest = self._create()
        with self.assertRaises(SQLiteBackupError):
            restore_drill(backup_path=backup, manifest_path=manifest, destination_db=restore_alias, environment="preview")

    def test_preview_restore_rejects_live_outside_symlink_traversal_and_overwrite(self):
        backup, manifest = self._create()
        for destination in (self.source, self.backup_root / "not-a-drill.sqlite3"):
            with self.subTest(destination=destination), self.assertRaises(SQLiteBackupError):
                restore_drill(backup_path=backup, manifest_path=manifest, destination_db=destination, environment="preview")
        nested = self.drill_directory / "nested"
        nested.mkdir()
        if os.name == "posix":
            nested.chmod(0o700)
        with self.assertRaises(SQLiteBackupError):
            restore_drill(backup_path=backup, manifest_path=manifest, destination_db=nested / ".." / "restored.sqlite3", environment="preview")
        target = self.drill_directory / "existing.sqlite3"
        target.write_bytes(b"winner")
        with self.assertRaises(SQLiteBackupError):
            restore_drill(backup_path=backup, manifest_path=manifest, destination_db=target, environment="preview")
        link = self.drill_directory / "restore-link.sqlite3"
        try:
            link.symlink_to(self.root / "outside-restore.sqlite3")
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(SQLiteBackupError):
                restore_drill(backup_path=backup, manifest_path=manifest, destination_db=link, environment="preview")

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_preview_artifacts_are_0600_and_directories_must_be_0700(self):
        backup, manifest = self._create()
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup_root.stat().st_mode), 0o700)
        self.backup_root.chmod(0o755)
        with self.assertRaises(SQLiteBackupError):
            create_backup(source_db_path=self.source, destination_directory=self.backup_root, environment="preview")

"""The user-facing entry point must never replace absent real data with a demo."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import BytesIO, StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

import run_preview
from region_model import __main__ as cli


class ActualRunnerTests(unittest.TestCase):
    def test_missing_real_inputs_stop_without_build_or_browser(self):
        errors = StringIO()
        with patch.object(Path, "is_file", return_value=False), \
             patch.object(run_preview, "main") as build, \
             patch.object(run_preview.webbrowser, "open") as browser, \
             redirect_stderr(errors):
            self.assertEqual(run_preview.run(), 2)
        build.assert_not_called()
        browser.assert_not_called()
        self.assertIn("buildings.gpkg", errors.getvalue())
        self.assertIn("합성 데이터로 대체하지 않습니다", errors.getvalue())

    def test_prepared_inputs_build_one_actual_400m_map(self):
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(run_preview, "main", return_value=0) as build, \
             patch.object(run_preview.webbrowser, "open") as browser, \
             redirect_stdout(StringIO()):
            self.assertEqual(run_preview.run(), 0)
        argv = build.call_args.args[0]
        self.assertEqual(argv[0], "build")
        self.assertEqual(argv[argv.index("--size") + 1], "400")
        self.assertNotIn("demo", argv)
        self.assertEqual(Path(argv[argv.index("--provenance") + 1]).name, "provenance.json")
        self.assertEqual(Path(argv[argv.index("--buildings") + 1]).name, "buildings.gpkg")
        output = Path(argv[argv.index("--out") + 1])
        self.assertEqual(output.parent, run_preview.ROOT / "outputs")
        self.assertTrue(output.name.startswith("gangnam_actual400_"))
        browser.assert_called_once_with((output / "preview.html").as_uri(), new=2)

    def test_failed_actual_build_does_not_open_browser(self):
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(run_preview, "main", return_value=2), \
             patch.object(run_preview.webbrowser, "open") as browser, \
             redirect_stdout(StringIO()):
            self.assertEqual(run_preview.run(), 2)
        browser.assert_not_called()

    def test_cli_build_defaults_to_single_400m_extent(self):
        with patch.object(cli, "build_one") as build, \
             patch.object(Path, "exists", return_value=False):
            result = cli.main([
                "build", "--buildings", "buildings.gpkg", "--schema", "schema.json",
                "--out", "unused-test-output",
            ])
        self.assertEqual(result, 0)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[0].size, 400)
        self.assertEqual(build.call_args.args[0].out, Path("unused-test-output"))

    def test_cli_rejects_actual_1000m_build(self):
        with patch.object(cli, "build_one") as build, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main([
                    "build", "--buildings", "buildings.gpkg", "--schema", "schema.json",
                    "--size", "1000", "--out", "unused-test-output",
                ])
        self.assertEqual(error.exception.code, 2)
        build.assert_not_called()


class ProvenanceTests(unittest.TestCase):
    def sample_provenance(self):
        return {
            "dataset_id": "gangnam_actual_400", "is_synthetic": False,
            "center_lonlat": [127.0276, 37.4979], "size_m": 400,
            "buildings": {
                "file": "buildings.gpkg", "sha256": hashlib.sha256(b"building bytes").hexdigest(),
                "center_lonlat": [127.0276, 37.4979], "size_m": 400, "is_synthetic": False,
                "source_name": "Verified source", "source_url": "https://example.org/dataset", "snapshot_date": "2026-09-09",
            },
            "background": {
                "file": "background.gpkg", "sha256": hashlib.sha256(b"background bytes").hexdigest(),
                "source": "OpenStreetMap via OSMnx", "synthetic": False, "status": "ok", "buildings_used": False,
                "center_lonlat": [127.0276, 37.4979], "size_m": 400,
            },
        }

    def verify(self):
        return cli.read_verified_provenance(
            Path("provenance.json"), [127.0276, 37.4979], 400,
            Path("buildings.gpkg"), Path("background.gpkg"),
        )

    def test_matching_extent_and_bytes_preserve_portable_provenance(self):
        provenance = self.sample_provenance()
        with patch.object(cli, "read_json", return_value=provenance), \
             patch.object(Path, "open", side_effect=[BytesIO(b"building bytes"), BytesIO(b"background bytes")]):
            self.assertEqual(self.verify(), provenance)

    def test_modified_dataset_bytes_are_rejected(self):
        with patch.object(cli, "read_json", return_value=self.sample_provenance()), \
             patch.object(Path, "open", return_value=BytesIO(b"modified building bytes")):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                self.verify()

    def test_wrong_extent_or_synthetic_claim_is_rejected_before_file_reads(self):
        for key, value in (("is_synthetic", True), ("center_lonlat", [127.0, 37.5]), ("size_m", 1000)):
            provenance = self.sample_provenance()
            provenance[key] = value
            with self.subTest(field=key), patch.object(cli, "read_json", return_value=provenance), \
                 patch.object(Path, "open") as open_file:
                with self.assertRaises(ValueError):
                    self.verify()
                open_file.assert_not_called()

    def test_absolute_paths_cannot_enter_shared_metadata(self):
        provenance = self.sample_provenance()
        provenance["buildings"]["source_file"] = "C:/Users/private/buildings.shp"
        with patch.object(cli, "read_json", return_value=provenance), \
             patch.object(Path, "open", side_effect=[BytesIO(b"building bytes"), BytesIO(b"background bytes")]):
            with self.assertRaisesRegex(ValueError, "절대 로컬 경로"):
                self.verify()

    def test_background_extent_must_match_the_bundle(self):
        provenance = self.sample_provenance()
        provenance["background"]["center_lonlat"] = [127.1, 37.5]
        with patch.object(cli, "read_json", return_value=provenance), \
             patch.object(Path, "open", return_value=BytesIO(b"building bytes")):
            with self.assertRaisesRegex(ValueError, "background.*중심점"):
                self.verify()

    def test_schema_source_header_is_bound_to_checked_provenance(self):
        provenance = self.sample_provenance()
        schema = {"id_field": "A1", "height_field": "A16"}
        bound = cli.bind_schema_provenance(schema, provenance)
        self.assertEqual(bound["source_name"], provenance["buildings"]["source_name"])
        self.assertEqual(bound["source_url"], provenance["buildings"]["source_url"])
        self.assertEqual(bound["snapshot_date"], provenance["buildings"]["snapshot_date"])
        self.assertEqual(bound["height_field"], "A16")
        self.assertNotIn("source_name", schema)
        with self.assertRaisesRegex(ValueError, "snapshot_date"):
            cli.bind_schema_provenance({**schema, "snapshot_date": "2000-01-01"}, provenance)


if __name__ == "__main__":
    unittest.main()

from comfyui_turing_utils.workspace import transactions
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT))

from functools import partial
import json
from unittest import mock
from comfyui_turing_utils.workspace.material_execution import (
    read_material,
    publish_material,
)
from comfyui_turing_utils.workspace.materials import read_selected
from comfyui_turing_utils.workspace.directories import (
    list_directory,
    remove_empty_directory,
)
from comfyui_turing_utils.workspace.store import Project, Conflict, inside
from comfyui_turing_utils.workspace.editor import select_material
from comfyui_turing_utils.workspace import workflow_files
from comfyui_turing_utils.workspace.templates import material_template
import torch
from turing_test_support.workspace import WorkspaceCase


class ProjectStorageTest(WorkspaceCase):
    def test_publication_recovers_before_and_after_json_write(self):
        for written in (False, True):
            with self.subTest(written=written):
                target = "recover-" + str(written)
                run = self.project.begin_run(target, 0)
                original = self.project.select

                def fail(*args, **kwargs):
                    if written:
                        original(*args, **kwargs)
                    raise OSError("simulated storage interruption")

                with mock.patch.object(self.project, "select", side_effect=fail):
                    with self.assertRaises(OSError):
                        self.project.finish_run(run, text="saved")
                self.assertEqual(self.project.run(run)["status"], "publishing")
                reopened = Project("test")
                self.assertEqual(
                    reopened.selections()[target], {"text": "saved", "revision": 1}
                )
                self.assertEqual(reopened.run(run)["status"], "success")
                self.assertTrue(reopened.finish_run(run, text="ignored retry"))
                self.assertEqual(reopened.selections()[target]["revision"], 1)

    def test_publication_recovery_preserves_newer_manual_selection(self):
        run = self.project.begin_run("recovery", 0)
        with mock.patch.object(
            self.project, "select", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                self.project.finish_run(run, text="generated")
        self.project.select("recovery", {"text": "manual"})
        reopened = Project("test")
        self.assertEqual(reopened.selections()["recovery"]["text"], "manual")
        self.assertFalse(reopened.run(run)["selected"])

    def test_database_upgrade_preserves_materials_and_selections(self):
        self.project.select("upgrade", {"text": "kept"})
        run = self.project.begin_run("upgrade", 1)
        with self.project.connect() as db:
            db.execute("DROP INDEX run_status")
            db.execute("DROP TABLE operations")
            db.execute("ALTER TABLE runs DROP COLUMN dependency")
            db.execute("ALTER TABLE runs DROP COLUMN error")
            db.execute("PRAGMA user_version=1")
        reopened = Project("test")
        self.assertEqual(reopened.selections()["upgrade"]["text"], "kept")
        self.assertEqual(reopened.run(run)["status"], "prepared")
        with reopened.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIn(
                "run_status",
                {row["name"] for row in db.execute("PRAGMA index_list(runs)")},
            )

    def test_open_does_not_initialize_invalid_project(self):
        root = Path(self.tmp.name) / "invalid"
        root.mkdir()
        path = root / "canvas.json"
        path.write_text('{"format":2}')
        with self.assertRaises(ValueError):
            Project("invalid")
        self.assertFalse((root / "workspace.sqlite3").exists())
        self.assertEqual(path.read_text(), '{"format":2}')

    def test_v3_migration_preserves_values_and_original_files(self):
        workflow, graph = material_template(("text",))
        ports = json.loads(graph["1"]["inputs"]["ports"])
        ports.append(
            dict(
                id="steps",
                slot=1,
                name="Steps",
                kind="parameter",
                type="INT",
                default=6,
            )
        )
        graph["1"]["inputs"]["ports"] = json.dumps(ports)
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "A"
        )
        path = workflow_files.card_path(self.project.root, identity)
        original_workflow = workflow_files.load_json(path)
        canvas = workflow_files.load_json(self.project.root / "canvas.json")
        canvas.pop("root")
        canvas.update(
            format=3,
            settings={"name": "Migrated", "max_megapixels": 2.0},
            selections={
                identity + ":" + graph["3"]["inputs"]["stub_id"]: {
                    "text": "selected",
                    "revision": 7,
                }
            },
        )
        canvas["cards"][0]["parameters"] = {"steps": 9}
        transactions.atomic_json(self.project.root / "canvas.json", canvas)
        reopened = Project("test")
        saved = workflow_files.load_json(self.project.root / "canvas.json")
        self.assertEqual(saved["format"], 4)
        self.assertNotIn("selections", saved)
        self.assertNotIn("parameters", saved["cards"][0])
        self.assertEqual(
            workflow_files.read_instance(reopened.root, identity)["parameters"],
            {"steps": 9},
        )
        self.assertEqual(reopened.settings()["name"], "Migrated")
        self.assertEqual(
            workflow_files.load_json(self.project.root / ".canvas-v3.json"), canvas
        )
        self.assertEqual(
            workflow_files.load_json(
                self.project.root / ".canvas-v3" / "cards" / identity / "workflow.json"
            ),
            original_workflow,
        )
        before = path.read_bytes()
        Project("test")
        self.assertEqual(path.read_bytes(), before)

    def test_trim_persists_and_does_not_block_a_new_publication(self):
        workflow, graph = material_template(("audio",))
        identity = workflow_files.add_instance(
            self.project.root, workflow_files.pack(workflow, graph), "Audio"
        )
        target = identity + ":" + graph["3"]["inputs"]["stub_id"]
        audio = {"waveform": torch.zeros(1, 2, 44100), "sample_rate": 44100}
        writer = partial(publish_material, "audio")
        run = self.project.begin_run(target, 0)
        writer("test", target, run, 0, "audio", audio)
        source = self.project.selection(target)
        original = self.project.path(source["asset"]).read_bytes()
        run = self.project.begin_run(target, 1)
        select_material(
            self.project,
            identity,
            graph["3"]["inputs"]["stub_id"],
            {"start": 0.25, "end": 0.5},
            source["revision"],
        )
        reader = partial(read_material, "audio")
        value = reader(directory="test", target=target, start=0, end=0)[0]
        self.assertEqual(value["waveform"].shape[-1], 11025)
        self.assertEqual(self.project.path(source["asset"]).read_bytes(), original)
        self.assertTrue(
            writer("test", target, run, 1, "audio", audio)["ui"]["material"][0][
                "selected"
            ]
        )
        selected = Project("test").selection(target)
        self.assertEqual((selected["start"], selected["end"]), (0.25, 0.5))

    def test_project_settings_pixel_limit_and_conflict(self):
        self.assertEqual(self.project.settings()["max_megapixels"], 4.0)
        revision = self.project.save_settings(
            {"name": "Scene", "max_megapixels": 0.001}, 0
        )
        self.assertEqual(revision, 1)
        with self.assertRaises(Conflict):
            self.project.save_settings({"name": "Other", "max_megapixels": 4.0}, 0)
        for pixels in (0, float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                self.project.save_settings(
                    {"name": "Scene", "max_megapixels": pixels}, 1
                )
        run = self.project.begin_run("image", 0)
        asset = publish_material(
            "image", "test", "image", run, 0, "image", torch.ones(1, 64, 64, 3)
        )["result"][0]
        value = read_selected("image", "test", asset)[0]
        self.assertLessEqual(value.shape[1] * value.shape[2], 0.001 * 1024 * 1024)

    def test_directory_listing_and_empty_tree_deletion(self):
        folder = Path(self.tmp.name) / "empty"
        (folder / "nested/child").mkdir(parents=True)
        self.assertFalse(list_directory("empty")["empty"])
        remove_empty_directory("empty")
        self.assertFalse(folder.exists())
        with self.assertRaises(ValueError):
            remove_empty_directory("test")
        self.assertTrue((self.project.root / "canvas.json").exists())
        for name in ("", "../", "/"):
            with self.assertRaises(ValueError):
                remove_empty_directory(name)
        (Path(self.tmp.name) / "link").symlink_to(
            self.project.root, target_is_directory=True
        )
        with self.assertRaises(ValueError):
            remove_empty_directory("link")

    def test_empty_text_and_explicit_project_creation(self):
        self.project.select("empty", {"text": ""})
        self.assertEqual(self.project.selections()["empty"]["text"], "")
        root = Path(self.tmp.name) / "other"
        with self.assertRaises(ValueError):
            Project("other")
        self.assertFalse(root.exists())
        root.mkdir()
        (root / "keep.txt").write_text("user data")
        with self.assertRaises(ValueError):
            Project.create("other")
        self.assertFalse((root / "canvas.json").exists())
        self.assertEqual((root / "keep.txt").read_text(), "user data")
        with self.assertRaises(ValueError):
            Project.create("test")

    def test_conflicting_result_stays_in_history(self):
        run = self.project.begin_run("3", 0)
        self.project.select("3", {"text": "manual"})
        self.assertFalse(self.project.finish_run(run, text="generated"))
        self.assertEqual(self.project.document()["selections"]["3"]["text"], "manual")
        self.assertFalse(self.project.finish_run(run, text="generated"))
        self.assertEqual(len(self.project.history("text")), 0)

    def test_document_compare_and_swap(self):
        self.project.patch(0, [])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.project.patch(0, [])

    def test_paths(self):
        for path in ("../escape", "/absolute", "", "folder\\escape"):
            with self.assertRaises(ValueError):
                inside(self.tmp.name, path)

    def test_text_writer(self):
        run = self.project.begin_run("3", 0)
        result = publish_material("text", "test", "3", run, 0, "prompt", "new")
        self.assertTrue(result["ui"]["material"][0]["selected"])
        self.assertEqual(result["result"][0], "new")
        self.assertEqual(self.project.selections()["3"]["text"], "new")
        self.assertFalse((self.project.root / "materials/text").exists())

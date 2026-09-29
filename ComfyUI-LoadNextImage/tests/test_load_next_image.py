import importlib.util
import base64
from io import BytesIO
import base64
from io import BytesIO
import os
import stat
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

try:
    import folder_paths  # noqa: F401
except ImportError:
    sys.modules["folder_paths"] = types.SimpleNamespace(
        get_input_directory=lambda: tempfile.gettempdir()
    )

try:
    import server  # noqa: F401
except ImportError:
    class _TestRoutes:
        def get(self, route):
            return lambda function: function

    sys.modules["server"] = types.SimpleNamespace(
        PromptServer=types.SimpleNamespace(
            instance=types.SimpleNamespace(routes=_TestRoutes())
        )
    )

try:
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        web=types.SimpleNamespace(json_response=lambda *args, **kwargs: None)
    )

MODULE_PATH = Path(__file__).parents[1] / "load_next_image.py"
SPEC = importlib.util.spec_from_file_location("load_next_image_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LoadNextImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        from PIL import Image

        Image.new("RGBA", (5, 3), (20, 80, 160, 100)).save(self.directory / "image1.png")
        Image.new("RGB", (4, 2), (160, 80, 20)).save(self.directory / "image2.png")
        Image.new("RGB", (3, 2), (80, 160, 20)).save(self.directory / "image10.png")
        (self.directory / "ignored.txt").write_text("not an image", encoding="utf-8")
        (self.directory / "nested").mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def execute(self):
        return MODULE.LoadNextImageFromDirectory().load_next_image(str(self.directory))

    def test_natural_order_batch_and_wraparound(self):
        first = self.execute()
        second = self.execute()
        third = self.execute()
        wrapped = self.execute()

        self.assertEqual(
            [first["result"][4], second["result"][4], third["result"][4], wrapped["result"][4]],
            ["image1.png", "image2.png", "image10.png", "image1.png"],
        )
        self.assertEqual([first["result"][2], second["result"][2], third["result"][2]], [0, 1, 2])
        self.assertEqual([first["result"][3], second["result"][3], third["result"][3]], [1, 2, 0])
        self.assertEqual(first["result"][0].shape, (1, 3, 5, 3))
        self.assertEqual(first["result"][1].shape, (1, 3, 5))
        self.assertAlmostEqual(float(first["result"][1][0, 0, 0]), 1 - 100 / 255)
        self.assertEqual(float(second["result"][1].max()), 0.0)
        self.assertEqual(first["result"][0].dtype, MODULE.torch.float32)
        self.assertEqual((self.directory / "index.txt").read_text(encoding="utf-8").strip(), "1")

    def test_invalid_index_resets_and_no_images_does_not_change_index(self):
        (self.directory / "index.txt").write_text("-1", encoding="utf-8")
        selected = self.execute()
        self.assertEqual(selected["result"][2:4], (0, 1))

        empty = self.directory / "empty"
        empty.mkdir()
        (empty / "index.txt").write_text("2", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "No supported images"):
            MODULE.LoadNextImageFromDirectory().load_next_image(str(empty))
        self.assertEqual((empty / "index.txt").read_text(encoding="utf-8"), "2")

    def test_manual_filename_override_is_used_once(self):
        first = MODULE.LoadNextImageFromDirectory().load_next_image(
            str(self.directory), image="image10.png"
        )
        following = self.execute()

        self.assertEqual(first["result"][2:5], (2, 0, "image10.png"))
        self.assertTrue(first["ui"]["used_image_override"][0])
        self.assertEqual(
            float(first["result"][0][0, 0, 0, 0]),
            80 / 255,
        )
        self.assertEqual((self.directory / "index.txt").read_text(encoding="utf-8").strip(), "0")
        self.assertEqual(following["result"][2:5], (0, 1, "image1.png"))
        self.assertFalse(following["ui"]["used_image_override"][0])

    def test_preview_and_image_match_the_selected_filename(self):
        result = MODULE.LoadNextImageFromDirectory().load_next_image(
            str(self.directory), image="image10.png"
        )
        image_tensor, _, current_index, next_index, filename = result["result"][:5]

        self.assertEqual((current_index, next_index, filename), (2, 0, "image10.png"))
        self.assertAlmostEqual(float(image_tensor[0, 0, 0, 0]), 80 / 255)
        self.assertAlmostEqual(float(image_tensor[0, 0, 0, 1]), 160 / 255)
        self.assertAlmostEqual(float(image_tensor[0, 0, 0, 2]), 20 / 255)

        preview_data = base64.b64decode(result["ui"]["preview"][0])
        from PIL import Image

        with Image.open(BytesIO(preview_data)) as preview:
            self.assertEqual(preview.size, (3, 2))
            for actual, expected in zip(preview.getpixel((1, 1)), (80, 160, 20)):
                self.assertLessEqual(abs(actual - expected), 30)

    def test_preview_selection_does_not_advance_persistent_index(self):
        index_path = self.directory / "index.txt"
        index_path.write_text("1", encoding="utf-8")

        preview = MODULE._preview_image_selection(str(self.directory), "image10.png")

        self.assertEqual(preview["current_index"], 2)
        self.assertEqual(preview["next_index"], 0)
        self.assertEqual(preview["filename"], "image10.png")
        self.assertEqual(index_path.read_text(encoding="utf-8"), "1")

    def test_missing_filename_does_not_change_index(self):
        (self.directory / "index.txt").write_text("1", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "is not present"):
            MODULE.LoadNextImageFromDirectory().load_next_image(
                str(self.directory), image="missing.png"
            )
        self.assertEqual((self.directory / "index.txt").read_text(encoding="utf-8"), "1")

    def test_index_write_failure_still_returns_image_and_warning(self):
        with patch.object(
            MODULE,
            "_write_index_atomically",
            side_effect=OSError("access denied"),
        ):
            result = self.execute()

        self.assertEqual(result["result"][0].shape, (1, 3, 5, 3))
        self.assertEqual(result["result"][1].shape, (1, 3, 5))
        self.assertEqual(result["result"][2:5], (0, 1, "image1.png"))
        self.assertIn("access denied", result["ui"]["warning"][0])
        self.assertFalse((self.directory / "index.txt").exists())

    def test_simultaneous_executions_reserve_distinct_indexes(self):
        (self.directory / "index.txt").write_text("0", encoding="utf-8")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.execute()["result"][2], range(4)))

        self.assertEqual(sorted(results), [0, 1, 2, 3])
        self.assertEqual((self.directory / "index.txt").read_text(encoding="utf-8").strip(), "1")

    def test_failed_image_load_does_not_advance_index(self):
        (self.directory / "image1.png").write_bytes(b"not a valid image")
        (self.directory / "index.txt").write_text("0", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "Could not load or prepare selected image"):
            self.execute()
        self.assertEqual((self.directory / "index.txt").read_text(encoding="utf-8"), "0")

    @unittest.skipUnless(os.name == "nt", "Windows read-only replacement behavior")
    def test_read_only_index_is_made_writable_before_atomic_replace(self):
        index_path = self.directory / "index.txt"
        index_path.write_text("0", encoding="utf-8")
        index_path.chmod(index_path.stat().st_mode & ~stat.S_IWRITE)

        try:
            result = self.execute()
            self.assertEqual(result["result"][2:4], (0, 1))
            self.assertEqual(index_path.read_text(encoding="utf-8").strip(), "1")
        finally:
            if index_path.exists():
                index_path.chmod(index_path.stat().st_mode | stat.S_IWRITE)


if __name__ == "__main__":
    unittest.main()

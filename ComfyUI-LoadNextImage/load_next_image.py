from __future__ import annotations

import base64
import errno
import logging
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import folder_paths
import numpy as np
import torch
from PIL import Image, ImageOps
from aiohttp import web
from server import PromptServer


LOGGER = logging.getLogger("comfyui.load_next_image")
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
NATURAL_SORT_PARTS = re.compile(r"(\d+)")
NEXT_IMAGE_OPTION = "Next image (saved index)"


def _natural_sort_key(filename: str) -> tuple[tuple[tuple[int, object], ...], str, str]:
    parts: list[tuple[int, object]] = []
    for part in NATURAL_SORT_PARTS.split(filename):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part.casefold()))
    return tuple(parts), filename.casefold(), filename


def _list_images(directory: Path) -> list[Path]:
    try:
        with os.scandir(directory) as entries:
            images = [
                Path(entry.path)
                for entry in entries
                if entry.is_file() and Path(entry.name).suffix.casefold() in SUPPORTED_EXTENSIONS
            ]
    except PermissionError as exc:
        raise PermissionError(f"Cannot read image directory: {directory}") from exc
    except OSError as exc:
        raise OSError(f"Cannot scan image directory '{directory}': {exc}") from exc

    return sorted(images, key=lambda path: _natural_sort_key(path.name))


@contextmanager
def _locked_index(directory: Path) -> Iterator[None]:
    lock_path = directory / "index.txt.lock"
    try:
        lock_file = lock_path.open("a+b")
    except OSError as exc:
        raise OSError(f"Cannot create or open index lock file '{lock_path}': {exc}") from exc

    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()

            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if (
                        exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN)
                        and getattr(exc, "winerror", None) not in (33, 36)
                    ):
                        raise
                    time.sleep(0.05)

            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _read_index(index_path: Path) -> int:
    try:
        value = index_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise OSError(f"Cannot read index file '{index_path}': {exc}") from exc

    try:
        return int(value)
    except ValueError:
        return -1


def _write_index_atomically(index_path: Path, index: int) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".index.txt.",
            suffix=".tmp",
            dir=index_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write(f"{index}\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        try:
            os.replace(temporary_path, index_path)
        except OSError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) != 5
                or not index_path.exists()
            ):
                raise

            original_mode = index_path.stat().st_mode
            if original_mode & stat.S_IWRITE:
                raise

            os.chmod(index_path, original_mode | stat.S_IWRITE)
            try:
                os.replace(temporary_path, index_path)
            except OSError as replace_error:
                try:
                    os.chmod(index_path, original_mode)
                except OSError as restore_error:
                    raise OSError(
                        f"{replace_error}; could not restore the original read-only state: {restore_error}"
                    ) from replace_error
                raise
        temporary_path = None
    except OSError as exc:
        help_text = ""
        if os.name == "nt" and getattr(exc, "winerror", None) in (5, 32, 33):
            help_text = (
                " Windows denied replacing the index. Ensure the ComfyUI process has modify and "
                "delete permission for the directory and index.txt, remove its read-only attribute, "
                "and close any application that may be holding the file open."
            )
        raise OSError(f"Cannot safely save index file '{index_path}': {exc}.{help_text}") from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _make_preview(image: Image.Image) -> str:
    from io import BytesIO

    preview = image.copy()
    preview.thumbnail((384, 384), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    preview.save(buffer, format="JPEG", quality=80, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _preview_image_selection(directory: str, filename: str) -> dict:
    if not directory.strip():
        raise ValueError("Directory must be provided.")
    image_directory = Path(directory).expanduser().resolve()
    if not image_directory.exists():
        raise FileNotFoundError(f"Image directory does not exist: {image_directory}")
    if not image_directory.is_dir():
        raise NotADirectoryError(f"Image path is not a directory: {image_directory}")

    images = _list_images(image_directory)
    selected_index = next(
        (position for position, candidate in enumerate(images) if candidate.name == filename),
        -1,
    )
    if selected_index < 0:
        raise ValueError(f"Selected image '{filename}' is not present in '{image_directory}'.")

    selected_path = images[selected_index]
    try:
        with Image.open(selected_path) as source:
            source.load()
            preview_image = ImageOps.exif_transpose(source).convert("RGB")
        preview = _make_preview(preview_image)
    except Exception as exc:
        raise RuntimeError(f"Could not preview selected image '{selected_path}': {exc}") from exc

    total_images = len(images)
    return {
        "preview": preview,
        "current_index": selected_index,
        "next_index": (selected_index + 1) % total_images,
        "total_images": total_images,
        "filename": selected_path.name,
        "filepath": str(selected_path),
        "directory": str(image_directory),
    }


class LoadNextImageFromDirectory:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "directory": (
                    "STRING",
                    {
                        "default": folder_paths.get_input_directory(),
                        "tooltip": "Folder scanned on the ComfyUI server. The sequence is stored in its index.txt file.",
                    },
                ),
                "image": (
                    "STRING",
                    {
                        "default": NEXT_IMAGE_OPTION,
                        "tooltip": "Choose a listed image to load once, or continue with the saved index.",
                    },
                ),
            },
            "optional": {
                "refresh": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "label_on": "Refresh",
                        "label_off": "Refresh",
                        "tooltip": "Toggle to force a new workflow execution. Each actual execution advances the index once.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK", "INT", "INT", "STRING", "INT", "STRING")
    RETURN_NAMES = ("IMAGE", "MASK", "current_index", "next_index", "filename", "total_images", "filepath")
    FUNCTION = "load_next_image"
    CATEGORY = "image"

    @classmethod
    def IS_CHANGED(cls, directory: str, image: str = NEXT_IMAGE_OPTION, refresh: bool = False):
        # ComfyUI caches identical inputs; a NaN change token makes each queued run advance once.
        return float("nan")

    def load_next_image(
        self,
        directory: str,
        image: str = NEXT_IMAGE_OPTION,
        refresh: bool = False,
    ):
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("Directory must be a non-empty path on the ComfyUI server.")
        if not isinstance(image, str):
            raise ValueError("Image selection must be a filename or the saved-index option.")

        try:
            image_directory = Path(directory).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Cannot normalize image directory '{directory}': {exc}") from exc

        if not image_directory.exists():
            raise FileNotFoundError(f"Image directory does not exist: {image_directory}")
        if not image_directory.is_dir():
            raise NotADirectoryError(f"Image path is not a directory: {image_directory}")

        index_path = image_directory / "index.txt"
        with _locked_index(image_directory):
            images = _list_images(image_directory)
            total_images = len(images)
            if total_images == 0:
                raise RuntimeError(
                    f"No supported images found in '{image_directory}'. "
                    "Supported extensions: .png, .jpg, .jpeg, .webp, .bmp, .tiff (and .tif)."
                )

            saved_index = _read_index(index_path)
            if image == NEXT_IMAGE_OPTION or not image:
                selected_index = saved_index if 0 <= saved_index < total_images else 0
                used_image_override = False
            else:
                selected_index = next(
                    (position for position, candidate in enumerate(images) if candidate.name == image),
                    -1,
                )
                if selected_index < 0:
                    raise ValueError(
                        f"Selected image '{image}' is not present in '{image_directory}'. "
                        "Refresh the image list and choose an available file."
                    )
                used_image_override = True

            selected_path = images[selected_index]

            try:
                with Image.open(selected_path) as source:
                    source.load()
                    image = ImageOps.exif_transpose(source).convert("RGBA")
                image_array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
                image_tensor = torch.from_numpy(image_array.copy()).unsqueeze(0)
                alpha = np.asarray(image.getchannel("A"), dtype=np.float32) / 255.0
                mask_tensor = torch.from_numpy(1.0 - alpha).unsqueeze(0)
                preview_base64 = _make_preview(image.convert("RGB"))
            except Exception as exc:
                raise RuntimeError(
                    f"Could not load or prepare selected image '{selected_path}' "
                    f"(index {selected_index}): {exc}"
                ) from exc

            current_index = selected_index
            next_index = (selected_index + 1) % total_images
            index_warning = None
            try:
                _write_index_atomically(index_path, next_index)
            except OSError as exc:
                index_warning = str(exc)
                LOGGER.error(
                    "Loaded image '%s', but could not persist next index %d: %s",
                    selected_path,
                    next_index,
                    exc,
                )

        LOGGER.info(
            "Loaded image %d/%d from %s; next index is %d",
            current_index,
            total_images,
            selected_path,
            next_index,
        )

        return {
            "ui": {
                "preview": [preview_base64],
                "current_index": [current_index],
                "next_index": [next_index],
                "total_images": [total_images],
                "filename": [selected_path.name],
                "filepath": [str(selected_path)],
                "directory": [str(image_directory)],
                "used_image_override": [used_image_override],
                "warning": [index_warning or ""],
                "image_choices": [[path.name for path in images]],
            },
            "result": (
                image_tensor,
                mask_tensor,
                current_index,
                next_index,
                selected_path.name,
                total_images,
                str(selected_path),
            ),
        }


@PromptServer.instance.routes.get("/load_next_image/images")
async def list_directory_images(request):
    directory = request.query.get("directory", "").strip()
    if not directory:
        return web.json_response({"error": "Directory must be provided."}, status=400)

    try:
        image_directory = Path(directory).expanduser().resolve()
        if not image_directory.exists():
            return web.json_response(
                {"error": f"Image directory does not exist: {image_directory}"},
                status=400,
            )
        if not image_directory.is_dir():
            return web.json_response(
                {"error": f"Image path is not a directory: {image_directory}"},
                status=400,
            )
        return web.json_response(
            {
                "directory": str(image_directory),
                "images": [path.name for path in _list_images(image_directory)],
            }
        )
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.exception("Could not list images for selector in '%s'", directory)
        return web.json_response({"error": str(exc)}, status=400)


@PromptServer.instance.routes.get("/load_next_image/preview")
async def preview_directory_image(request):
    directory = request.query.get("directory", "").strip()
    filename = request.query.get("filename", "").strip()
    if not filename:
        return web.json_response({"error": "Filename must be provided."}, status=400)

    try:
        return web.json_response(_preview_image_selection(directory, filename))
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.exception("Could not preview '%s' from '%s'", filename, directory)
        return web.json_response({"error": str(exc)}, status=400)

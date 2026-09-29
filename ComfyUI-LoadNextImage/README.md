# Load Next Image From Directory

`LoadNextImageFromDirectory` loads one image per queued workflow execution and stores the next position in `index.txt` inside the chosen directory. It returns batch-size-1 `IMAGE` and `MASK` tensors, the selected/next indexes, filename, image count, and full path. The mask follows this repository's `PILToMask` convention: transparent pixels are `1`, opaque pixels are `0`, and images without alpha yield an all-zero mask.

## Installation

1. Copy the `ComfyUI-LoadNextImage` folder into `ComfyUI/custom_nodes/`.
2. Restart ComfyUI.
3. Find **Load Next Image From Directory** under **image**.

The ZIP is laid out with this folder at its root. The directory path is resolved by the ComfyUI server, not the browser, so it must be readable and writable by the ComfyUI process.

## Use

Enter an image directory and queue the workflow. Each execution loads the current item in natural filename order, then advances `index.txt`; after the last image, the next index wraps to `0`. Supported extensions are `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tiff`, and `.tif`. Subdirectories and other files are ignored.

The **image** input is a dropdown, not a text field, populated with supported image filenames from the chosen directory. Selecting a filename immediately displays that image and its current sorted index in the node preview, then queues one execution. That execution returns the selected file as `IMAGE`, reports the selected position as `current_index`, and only then saves the following position as `next_index` in `index.txt`. The preview request itself never advances or changes the persistent index. The dropdown resets to **Next image (saved index)** after a selected-file execution; leaving it there continues the saved sequence on subsequent runs. **Previous** and **Next** queue the adjacent image; **Previous** is available after the node has run for the current directory. Change the directory to refresh its image list.

The selected image is fully decoded and converted to both the `IMAGE` tensor and preview before the next index is calculated or saved. The preview and `current_index` therefore identify the same image that downstream nodes receive. Its compact thumbnail plus execution metadata are stored in the node properties so they appear again when a saved workflow is reopened. Rendering the node and restoring its preview never executes the loader or changes `index.txt`.

Connect `IMAGE` to downstream image processing or `Save Image`. The workflow in [`examples/load_next_image_workflow.json`](examples/load_next_image_workflow.json) demonstrates both.

## Index and safety behavior

- A missing, malformed, negative, or out-of-range index is treated as `0`.
- The index is read while holding a cross-process lock and is replaced atomically only after the selected image has loaded successfully.
- `index.txt.lock` is an internal sidecar used to coordinate simultaneous executions. Do not remove it while ComfyUI is running.
- An empty image directory or unreadable/invalid image raises a descriptive node error and does not advance the index.
- If loading succeeds but Windows or the filesystem refuses the index replacement, the node still returns the image, mask, and metadata. It logs the failure and displays a node warning; the saved index remains unchanged, so the next execution may select the same image.
- If the workflow is refreshed in the browser, the saved node preview is restored when the workflow itself was saved with the preview. Unsaved browser state is not recoverable after a page reload.

## Test procedure

1. Put at least three supported images in a writable test directory with names such as `image1.png`, `image2.png`, and `image10.png`. Connect the node to **Image Scale** and **Save Image**.
2. Remove `index.txt`, queue once, and confirm the preview says `Current Index: 0`, `Next Index: 1`, and `index.txt` contains `1`.
3. Queue repeatedly; confirm filename order is natural, the preview's current index matches the returned filename, and the index wraps from the last image to `0`.
4. Select a filename in the **image** selector; confirm its preview changes immediately without changing `index.txt`, then the queued execution processes that image and saves the following index. Confirm the selector returns to **Next image (saved index)** and the next execution continues from that saved position. Use **Previous** and **Next** to load adjacent images.
5. Save the workflow after execution, refresh the ComfyUI browser page, reopen that workflow, and confirm its image and execution details reappear without changing `index.txt`. Queue again and verify the next image is selected.
6. Restart ComfyUI and queue again. It should continue from the persisted index. To verify error handling, try an empty directory and confirm the node errors without modifying any existing `index.txt`.
7. To verify concurrency, queue two separate workflows/nodes targeting the same directory at the same time. Each successful execution should reserve a distinct successive index.

## Troubleshooting

- **Permission denied:** grant the ComfyUI process read access to the images and write access to the directory (needed for `index.txt`, its lock, and atomic replacement).
- **Windows `WinError 5` while saving `index.txt`:** the node automatically retries after clearing the read-only attribute when present. If the retry still fails, grant the ComfyUI process modify/delete permission on the directory and `index.txt`, remove any remaining read-only restriction, and close programs that may be holding the file open.
- **No supported images:** move supported image files directly into the selected directory; nested folders are not scanned.
- **An image cannot be opened:** verify the selected file is not corrupt and that Pillow supports its encoding.
- **Unexpected restart at zero:** inspect `index.txt`; invalid contents or an index outside the current image count deliberately reset the sequence.
- **Preview is blank:** queue the node once, ensure the JavaScript extension loaded after restart, and save the workflow so its preview metadata can be restored after a browser reload.

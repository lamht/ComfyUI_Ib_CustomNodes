import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXTENSION_NAME = "ComfyUI.LoadNextImageFromDirectory";
const PROPERTY_NAME = "loadNextImagePreview";
const NEXT_IMAGE_OPTION = "Next image (saved index)";
const PREVIEW_WIDTH = 280;
const PREVIEW_HEIGHT = 184;
const DETAILS_HEIGHT = 106;
const MIN_NODE_HEIGHT = 550;

function first(value) {
    return Array.isArray(value) ? value[0] : value;
}

function normalizePath(value) {
    return String(value || "").replaceAll("\\", "/").replace(/\/+$/, "").toLowerCase();
}

function nodeWidget(node, name) {
    return node.widgets?.find((widget) => widget.name === name);
}

function ensureImageCombo(node) {
    const selector = nodeWidget(node, "image");
    if (!selector) {
        console.error("[LoadNextImageFromDirectory] The image input widget was not found.");
        return null;
    }
    if (selector._loadNextImageCombo) {
        return selector;
    }

    const widgetIndex = node.widgets.indexOf(selector);
    const initialValue = selector.value || NEXT_IMAGE_OPTION;
    const existingCallback = selector.callback;
    node.widgets.splice(widgetIndex, 1);

    const combo = node.addWidget(
        "combo",
        "image",
        initialValue,
        (value) => {
            existingCallback?.(value);
            if (value !== NEXT_IMAGE_OPTION) {
                previewImageSelection(node, value);
                queueExecution();
            }
        },
        { values: [NEXT_IMAGE_OPTION] },
    );

    node.widgets.pop();
    node.widgets.splice(widgetIndex, 0, combo);
    combo._loadNextImageCombo = true;
    combo.options = combo.options || {};
    combo.options.values = [NEXT_IMAGE_OPTION];
    return combo;
}

async function updateImageChoices(node) {
    const directory = nodeWidget(node, "directory")?.value;
    const selector = ensureImageCombo(node);
    if (!selector || !directory) {
        return;
    }

    try {
        const response = await api.fetchApi(
            `/load_next_image/images?directory=${encodeURIComponent(directory)}`,
        );
        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(data.error || `Image list request failed (${response.status}).`);
        }

        if (normalizePath(nodeWidget(node, "directory")?.value) !== normalizePath(directory)) {
            return;
        }
        const values = [NEXT_IMAGE_OPTION, ...(data.images || [])];
        selector.options = selector.options || {};
        selector.options.values = values;
        if (!values.includes(selector.value)) {
            selector.value = NEXT_IMAGE_OPTION;
        }
        node._loadNextImageChoices = data.images || [];
        node.graph?.setDirtyCanvas(true, true);
    } catch (error) {
        console.error("[LoadNextImageFromDirectory] Could not refresh image choices.", error);
    }
}

function queueExecution() {
    try {
        const queued = app.queuePrompt(0, 1);
        queued?.catch((error) => {
            console.error("[LoadNextImageFromDirectory] Could not queue execution.", error);
        });
    } catch (error) {
        console.error("[LoadNextImageFromDirectory] Could not queue execution.", error);
    }
}

async function previewImageSelection(node, filename) {
    const directory = nodeWidget(node, "directory")?.value;
    if (!directory || !filename || filename === NEXT_IMAGE_OPTION) {
        return;
    }

    const requestId = (node._loadNextPreviewRequestId || 0) + 1;
    node._loadNextPreviewRequestId = requestId;
    try {
        const response = await api.fetchApi(
            `/load_next_image/preview?directory=${encodeURIComponent(directory)}&filename=${encodeURIComponent(filename)}`,
        );
        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(data.error || `Preview request failed (${response.status}).`);
        }
        if (node._loadNextPreviewRequestId !== requestId) {
            return;
        }

        const saved = {
            preview: data.preview,
            currentIndex: data.current_index,
            nextIndex: data.next_index,
            totalImages: data.total_images,
            filename: data.filename,
            filepath: data.filepath,
            directory: data.directory,
        };
        node.properties = node.properties || {};
        node.properties[PROPERTY_NAME] = saved;
        node._loadNextPreviewSource = data.preview;

        const image = new Image();
        image.onload = () => {
            if (node._loadNextPreviewRequestId !== requestId) {
                return;
            }
            node._loadNextPreviewImage = image;
            node.graph?.setDirtyCanvas(true, true);
        };
        image.onerror = () => {
            console.error("[LoadNextImageFromDirectory] Could not display the selected image preview.");
        };
        image.src = `data:image/jpeg;base64,${data.preview}`;
        node.graph?.setDirtyCanvas(true, true);
    } catch (error) {
        if (node._loadNextPreviewRequestId === requestId) {
            console.error("[LoadNextImageFromDirectory] Could not preview selected image.", error);
        }
    }
}

function queueNavigation(node, direction) {
    const saved = node.properties?.[PROPERTY_NAME];
    const directory = nodeWidget(node, "directory")?.value;
    const sameDirectory = saved && normalizePath(saved.directory) === normalizePath(directory);
    const choices = node._loadNextImageChoices || [];
    let choice = NEXT_IMAGE_OPTION;

    if (sameDirectory && Number.isInteger(saved.totalImages) && saved.totalImages > 0) {
        const selectedIndex = direction > 0
            ? saved.nextIndex
            : (saved.currentIndex - 1 + saved.totalImages) % saved.totalImages;
        choice = choices[selectedIndex];
        if (!choice) {
            console.error("[LoadNextImageFromDirectory] The image list is out of date; refresh it and retry.");
            return;
        }
    } else if (direction < 0) {
        console.warn("[LoadNextImageFromDirectory] Run the node once before using Previous.");
        return;
    }

    const selector = nodeWidget(node, "image");
    if (!selector) {
        console.error("[LoadNextImageFromDirectory] The image selector widget was not found.");
        return;
    }

    selector.value = choice;
    node.graph?.setDirtyCanvas(true, true);
    queueExecution();
}

function restorePreview(node) {
    const saved = node.properties?.[PROPERTY_NAME];
    if (!saved?.preview || node._loadNextPreviewSource === saved.preview) {
        return;
    }

    node._loadNextPreviewSource = saved.preview;
    const image = new Image();
    image.onload = () => {
        node._loadNextPreviewImage = image;
        app.graph?.setDirtyCanvas(true, true);
    };
    image.onerror = () => {
        console.warn("[LoadNextImageFromDirectory] Could not restore the saved preview.");
        node._loadNextPreviewImage = null;
    };
    image.src = `data:image/jpeg;base64,${saved.preview}`;
}

app.registerExtension({
    name: EXTENSION_NAME,

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "LoadNextImageFromDirectory") {
            return;
        }

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            this.properties = this.properties || {};
            this.size = this.size || [PREVIEW_WIDTH + 32, MIN_NODE_HEIGHT];
            this.size[0] = Math.max(this.size[0], PREVIEW_WIDTH + 32);
            this.size[1] = Math.max(this.size[1], MIN_NODE_HEIGHT);
            this.addWidget("button", "Previous", null, () => queueNavigation(this, -1));
            this.addWidget("button", "Next", null, () => queueNavigation(this, 1));

            ensureImageCombo(this);

            const directoryWidget = nodeWidget(this, "directory");
            if (directoryWidget) {
                const existingCallback = directoryWidget.callback;
                directoryWidget.callback = (value) => {
                    existingCallback?.(value);
                    updateImageChoices(this);
                };
            }

            updateImageChoices(this);
            restorePreview(this);
            return result;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = onConfigure?.apply(this, arguments);
            this.size[0] = Math.max(this.size[0], PREVIEW_WIDTH + 32);
            this.size[1] = Math.max(this.size[1], MIN_NODE_HEIGHT);
            ensureImageCombo(this);
            updateImageChoices(this);
            restorePreview(this);
            return result;
        };

        nodeType.prototype.onExecuted = function (message) {
            const preview = first(message?.preview);
            if (!preview) {
                return;
            }

            this._loadNextPreviewRequestId = (this._loadNextPreviewRequestId || 0) + 1;
            const saved = {
                preview,
                currentIndex: first(message.current_index),
                nextIndex: first(message.next_index),
                totalImages: first(message.total_images),
                filename: first(message.filename),
                filepath: first(message.filepath),
                directory: first(message.directory),
                usedImageOverride: first(message.used_image_override),
                warning: first(message.warning),
            };
            this.properties = this.properties || {};
            this.properties[PROPERTY_NAME] = saved;
            this._loadNextPreviewSource = preview;

            const images = first(message.image_choices);
            if (Array.isArray(images)) {
                this._loadNextImageChoices = images;
                const selector = nodeWidget(this, "image");
                if (selector) {
                    selector.options = selector.options || {};
                    selector.options.values = [NEXT_IMAGE_OPTION, ...images];
                }
            }

            if (saved.usedImageOverride) {
                const selector = nodeWidget(this, "image");
                if (selector && selector.value === saved.filename) {
                    selector.value = NEXT_IMAGE_OPTION;
                }
            }

            const image = new Image();
            image.onload = () => {
                this._loadNextPreviewImage = image;
                this.size[0] = Math.max(this.size[0], PREVIEW_WIDTH + 32);
                this.size[1] = Math.max(this.size[1], MIN_NODE_HEIGHT);
                this.graph?.setDirtyCanvas(true, true);
            };
            image.onerror = () => {
                console.error("[LoadNextImageFromDirectory] The backend preview could not be displayed.");
            };
            image.src = `data:image/jpeg;base64,${preview}`;
            this.graph?.setDirtyCanvas(true, true);
        };

        const onDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            onDrawForeground?.apply(this, arguments);
            restorePreview(this);

            const saved = this.properties?.[PROPERTY_NAME];
            const image = this._loadNextPreviewImage;
            const width = Math.min(PREVIEW_WIDTH, this.size[0] - 24);
            const imageHeight = Math.min(PREVIEW_HEIGHT, width * 0.66);
            const left = (this.size[0] - width) / 2;
            const top = this.size[1] - DETAILS_HEIGHT - imageHeight - 18;

            ctx.save();
            ctx.fillStyle = "#17191d";
            ctx.strokeStyle = "#484d56";
            ctx.lineWidth = 1;
            ctx.fillRect(left, top, width, imageHeight);
            ctx.strokeRect(left, top, width, imageHeight);

            if (image?.complete && image.naturalWidth > 0) {
                const scale = Math.min(width / image.naturalWidth, imageHeight / image.naturalHeight);
                const drawWidth = image.naturalWidth * scale;
                const drawHeight = image.naturalHeight * scale;
                ctx.drawImage(
                    image,
                    left + (width - drawWidth) / 2,
                    top + (imageHeight - drawHeight) / 2,
                    drawWidth,
                    drawHeight,
                );
            } else {
                ctx.fillStyle = "#aeb4bf";
                ctx.font = "13px sans-serif";
                ctx.textAlign = "center";
                ctx.fillText("Run the node to load a preview", left + width / 2, top + imageHeight / 2);
            }

            if (saved) {
                const detailTop = top + imageHeight + 19;
                ctx.textAlign = "left";
                ctx.font = "12px sans-serif";
                ctx.fillStyle = "#e3e6eb";
                ctx.fillText(`Current Index: ${saved.currentIndex}`, left, detailTop);
                ctx.fillText(`Next Index: ${saved.nextIndex}`, left, detailTop + 16);
                ctx.fillText(`Total Images: ${saved.totalImages}`, left, detailTop + 32);
                ctx.fillStyle = "#b9c0ca";
                ctx.font = "11px sans-serif";
                ctx.fillText(`Filename: ${saved.filename || ""}`, left, detailTop + 51, width);
                if (saved.warning) {
                    ctx.fillStyle = "#ffb36b";
                    ctx.fillText("Warning: index was not saved", left, detailTop + 69, width);
                    this.size[1] = Math.max(this.size[1], MIN_NODE_HEIGHT + 20);
                }
            }
            ctx.restore();
        };
    },
});

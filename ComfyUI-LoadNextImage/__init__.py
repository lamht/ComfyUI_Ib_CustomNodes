from .load_next_image import LoadNextImageFromDirectory

NODE_CLASS_MAPPINGS = {
    "LoadNextImageFromDirectory": LoadNextImageFromDirectory,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadNextImageFromDirectory": "Load Next Image From Directory",
}

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

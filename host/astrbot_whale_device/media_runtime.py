"""Prepare the shared audio/video converter for the managed AstrBot process."""

import os
import shutil
from pathlib import Path


def prepare_media_environment(environment, binary_dir):
    """Make one converter available to all existing media consumers.

    Args:
        environment: Child process environment to extend.
        binary_dir: Managed, non-source directory for the executable.

    Returns:
        Whether an existing or bundled converter is available.
    """
    if shutil.which("ffmpeg", path=environment.get("PATH", "")):
        return True
    try:
        import imageio_ffmpeg
    except ImportError:
        return False
    source = Path(imageio_ffmpeg.get_ffmpeg_exe())
    target = Path(binary_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    environment["PATH"] = str(target.parent) + os.pathsep + environment.get("PATH", "")
    return True

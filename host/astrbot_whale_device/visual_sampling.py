"""Select changing frames and build a bounded, timestamped visual observation."""

import base64
import io

from PIL import Image, ImageChops, ImageDraw, ImageStat


class VisualSampler:
    def __init__(self, threshold=7.0, max_frames=6):
        self.threshold = threshold
        self.max_frames = max_frames
        self.frames = []
        self.previous = None
        self.sampled = 0

    def add(self, encoded, timestamp):
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as source:
            source.load()
            image = source.convert("RGB")
        self.sampled += 1
        signature = image.resize((64, 36)).convert("L")
        changed = (
            self.previous is None
            or ImageStat.Stat(ImageChops.difference(signature, self.previous)).mean[0]
            >= self.threshold
        )
        if len(self.frames) >= self.max_frames:
            return False
        if changed:
            image.thumbnail((480, 320))
            self.frames.append((float(timestamp), image))
            self.previous = signature
        return changed

    def result(self):
        if not self.frames:
            raise ValueError("no_frames_captured")
        width, height = 480, 344
        columns = min(2, len(self.frames))
        board = Image.new(
            "RGB",
            (width * columns, height * ((len(self.frames) + columns - 1) // columns)),
            "#18211e",
        )
        draw = ImageDraw.Draw(board)
        for i, (stamp, frame) in enumerate(self.frames):
            x, y = (i % columns) * width, (i // columns) * height
            board.paste(frame, (x + (width - frame.width) // 2, y + 24))
            draw.text((x + 10, y + 5), f"{stamp:.1f}s", fill="white")
        output = io.BytesIO()
        board.save(output, format="JPEG", quality=78)
        return base64.b64encode(output.getvalue()).decode()

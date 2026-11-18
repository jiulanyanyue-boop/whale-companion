"""Windows notification-area lifetime. UI changes are dispatched by the Tk poller."""
import queue


class DesktopTray:
    def __init__(self):
        self.events = queue.Queue()
        self.icon = None

    def start(self):
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new('RGBA', (64,64), '#248653')
        draw = ImageDraw.Draw(image)
        draw.ellipse((12,20,46,45), fill='white')
        draw.polygon([(42,32),(55,23),(55,43)],fill='white')
        draw.ellipse((22,27,26,31),fill='#248653')
        self.icon = pystray.Icon('WhaleAgent', image, 'Whale 手机助手', menu=pystray.Menu(
            pystray.MenuItem('打开主界面',lambda *_:self.events.put('show'),default=True),
            pystray.MenuItem('退出并停止主机',lambda *_:self.events.put('exit')),
        ))
        self.icon.run_detached()

    def stop(self):
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

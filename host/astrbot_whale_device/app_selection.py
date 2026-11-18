"""Selection state for the app permission picker; labels never become identifiers."""
from __future__ import annotations


class AppSelection:
    def __init__(self):
        self.apps = []
        self.selected: set[str] = set()
        self.saved: set[str] = set()

    def load(self, packages):
        self.selected = set(packages)
        self.saved = set(packages)

    def replace_apps(self, apps):
        self.apps = list(apps)

    def toggle(self, package, enabled):
        if enabled:
            self.selected.add(package)
        else:
            self.selected.discard(package)

    def visible(self, query='', mode='全部应用'):
        query = query.strip().casefold()
        return [app for app in self.apps
                if (not query or query in app.label.casefold() or query in app.package_name.casefold())
                and (mode != '已选择' or app.package_name in self.selected)
                and (mode != '未选择' or app.package_name not in self.selected)]

    @property
    def dirty(self):
        return self.selected != self.saved

    def packages(self):
        return sorted(self.selected)

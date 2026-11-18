"""Enable only the pinned local call bridge in NapCat's plugin allowlist."""

PLUGIN_ID = "napcat-plugin-maibot-qq-voice-call"
ANCHOR = 'dve = /* @__PURE__ */ new Set([\n  "napcat-plugin-builtin",'
PATCHED = ANCHOR + '\n  "' + PLUGIN_ID + '",'


def enable_call_bridge(source):
    """Extend the known allowlist without disabling other plugin checks.

    Args:
        source: Installed NapCat 4.18.42 source text.

    Returns:
        Updated source with only the call bridge added.

    Raises:
        ValueError: The installed version no longer matches the reviewed anchor.
    """
    if PATCHED in source:
        return source
    if source.count(ANCHOR) != 1:
        raise ValueError("napcat_plugin_allowlist_version_changed")
    return source.replace(ANCHOR, PATCHED, 1)

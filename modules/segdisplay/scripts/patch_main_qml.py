#!/usr/bin/env python3
"""Insert the front-panel hook into 240-MP's Main.qml.

Why an upstream file has to be touched at all: there is no global "current
module" or "current screen" anywhere in 240-MP. AppCore publishes no such
property or signal, and navigation is a QML-local stack around a single Loader,
so `moduleLoader.source` is the only thing that knows what is on screen. Two
lines hand it to modules/segdisplay/views/SegHook.qml, where all the actual
logic lives — so this patch should never need to change again.

Like the patched YouTube module, Main.qml belongs to upstream and is replaced by
every 240-MP upgrade. deploy-segdisplay.sh re-applies this; re-running it on an
already-patched file is a no-op.

Fails loudly when an anchor is missing. A silently unpatched Main.qml means a
box whose panel only ever shows the clock, with nothing to explain why.

usage: patch_main_qml.py <path to Main.qml>
"""

import sys

MARKER = "segdisplay-hook"

# Anchor 1: the comment that opens the module Loader block.
ANCHOR_LOADER = "    // --- MODULE LOADER ---\n"

BLOCK_LOADER = """    // segdisplay-hook BEGIN — front-panel reporter (modules/segdisplay).
    // Loaded from here rather than from a module because it has to outlive
    // moduleLoader's source changes in order to observe them. All behaviour
    // lives in SegHook.qml, so these lines should never need to change.
    Loader {
        id: segHook
        source: "modules/segdisplay/views/SegHook.qml"
        // Direct, not Qt.callLater: moduleLoader's source is a literal set at
        // creation, so it already reads correctly here, and a callLater
        // callback placed at this point was observed never running at all.
        // This fires the initial view; moduleLoader's onSourceChanged below
        // cannot, because it runs before this Loader has an item.
        onLoaded: if (item) item.setView(String(moduleLoader.source))
    }
    // segdisplay-hook END

"""

# Anchor 2: the Loader's initial source line, inside the Loader block.
ANCHOR_SOURCE = '        source: "views/ModuleList.qml";\n'

BLOCK_SOURCE = """        // segdisplay-hook BEGIN — the de facto "what screen am I on" signal.
        onSourceChanged: if (segHook.item) segHook.item.setView(String(source))
        // segdisplay-hook END
"""


def patch(text):
    for anchor, replacement in ((ANCHOR_LOADER, BLOCK_LOADER + ANCHOR_LOADER),
                                (ANCHOR_SOURCE, ANCHOR_SOURCE + BLOCK_SOURCE)):
        if anchor not in text:
            raise SystemExit(
                "ERROR: anchor not found in Main.qml:\n  %r\n"
                "Upstream changed shape. Re-derive the hook by hand before "
                "deploying — an unpatched Main.qml leaves the panel stuck on "
                "the clock with no error anywhere." % anchor)
        text = text.replace(anchor, replacement, 1)
    return text


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    with open(path) as fh:
        text = fh.read()

    if MARKER in text:
        print("already patched")
        return 0

    with open(path, "w") as fh:
        fh.write(patch(text))
    print("patched %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

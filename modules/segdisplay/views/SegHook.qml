import QtQuick

// SegHook — the app's side of the front-panel display.
//
// Loaded once, from Main.qml, and never navigated to. It exists because there
// is no global "current module" or "current screen" anywhere in 240-MP:
// AppCore publishes no such property or signal, and navigation is a QML-local
// stack around a single Loader (Main.qml). `moduleLoader.source` is the only
// thing that knows what is on screen, so the upstream patch hands it here and
// every other decision is made in files we own.
//
// It talks to scripts/segdisplay.py over loopback HTTP because that is the one
// outbound channel QML has: it cannot spawn a process and cannot write a file
// (QML_XHR_ALLOW_FILE_READ gates local GETs, and there is no local PUT at all).
//
// An Item rather than a QtObject purely so Loader is guaranteed to hold it.
Item {
    id: hook
    visible: false

    // Must match SEGDISPLAY_PORT in segdisplay.py.
    readonly property string endpoint: "http://127.0.0.1:8377/state"

    property bool playing: false

    // The only log line here, and it earns its place: on a headless box this is
    // the one thing that distinguishes "Main.qml was never patched" from "the
    // daemon is down". Neither failure produces any other output.
    Component.onCompleted: console.log("[SegDisplay] hook active -> " + endpoint)

    // In-flight requests, held only so the JS engine cannot collect them
    // mid-send. A QML XMLHttpRequest with no live reference is a known way to
    // lose a request: the socket opens and is reset before the headers go out,
    // and no callback ever fires.
    property var _pending: []

    // Log the first outcome of each kind and nothing after. The daemon may
    // legitimately be absent (display not wired yet, unit masked) and this runs
    // once a second during playback, so a per-failure message would be a wall
    // of text — but a permanently silent failure is worse, because nothing else
    // in the system says the panel is unreachable.
    property bool _loggedOk: false
    property bool _loggedFail: false

    function post(obj) {
        var xhr = new XMLHttpRequest()
        _pending.push(xhr)
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE)
                return
            var i = hook._pending.indexOf(xhr)
            if (i >= 0)
                hook._pending.splice(i, 1)
            if (xhr.status === 200) {
                if (!hook._loggedOk) {
                    hook._loggedOk = true
                    console.log("[SegDisplay] panel daemon reachable")
                }
            } else if (!hook._loggedFail) {
                hook._loggedFail = true
                console.log("[SegDisplay] panel daemon unreachable (HTTP " + xhr.status
                            + ") at " + hook.endpoint)
            }
        }
        xhr.open("POST", hook.endpoint)
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify(obj))
    }

    // moduleLoader.source is an absolute file:// URL. Reduce it to the form the
    // daemon matches on: "modules/<folder>/views/X.qml" for a module view,
    // "views/X.qml" for an app view.
    function relativeView(url) {
        var s = String(url)
        var i = s.lastIndexOf("/modules/")
        if (i >= 0)
            return s.substring(i + 1)
        var parts = s.split("/")
        return parts.slice(-2).join("/")
    }

    // Set by Main.qml's patch. The hook needs the Loader itself, not just its
    // source, to read the highlighted row out of whatever list is on screen.
    property var appLoader: null
    property string view: ""
    property var _list: null

    function setView(url) {
        hook.view = relativeView(url)
        hook.findList()
        hook.report()
    }

    // View and label travel together in one message, always. The daemon's HTTP
    // server is threaded, so two separate posts can be applied in either order,
    // and a label landing after the view that invalidated it would sit on the
    // panel until the next keypress.
    function report() {
        post({ view: hook.view, label: hook.currentLabel() })
    }

    // 240-MP's own screens are all "a ListView of things with a name" — the
    // module list, the settings list, a module's settings. None of them exposes
    // the highlighted row to the outside, and their ids are private to their
    // component, so the only way in is to walk the item tree. Deliberately
    // duck-typed rather than matched by id or type name: it then also works on
    // upstream screens that do not exist yet, and simply finds nothing (and
    // shows nothing) on the ones that are not lists.
    function findList() {
        hook._list = hook.appLoader ? hook.search(hook.appLoader.item, 0) : null
    }

    function search(obj, depth) {
        if (!obj || depth > 8)
            return null
        if (obj.currentIndex !== undefined && obj.model !== undefined
                && obj.model && obj.model.length !== undefined && obj.visible !== false)
            return obj
        var kids = obj.children
        if (!kids)
            return null
        for (var i = 0; i < kids.length; ++i) {
            var found = hook.search(kids[i], depth + 1)
            if (found)
                return found
        }
        return null
    }

    // Every list in the app names its rows differently — the module list uses
    // "name", the settings lists use "label". Try each rather than special-case
    // the screens by path.
    function currentLabel() {
        var list = hook._list
        if (!list || list.currentIndex < 0 || !list.model
                || list.currentIndex >= list.model.length)
            return ""
        var row = list.model[list.currentIndex]
        if (row === undefined || row === null)
            return ""
        if (typeof row === "string")
            return row
        return String(row.name || row.label || row.title || row.text || "")
    }

    // The Loader creates its item *after* source changes, so this — not
    // setView — is when the new screen's list actually exists to be found.
    Connections {
        target: hook.appLoader
        ignoreUnknownSignals: true
        function onItemChanged() {
            hook.findList()
            hook.report()
        }
    }

    // Rebound every time findList() runs, so the label follows the cursor
    // without polling.
    Connections {
        target: hook._list
        ignoreUnknownSignals: true
        function onCurrentIndexChanged() { hook.report() }
    }

    Connections {
        target: mpvController

        // Same signal Main.qml uses to suppress the screen saver, so this needs
        // no new C++ and covers every playback module — including ones we did
        // not write — for free.
        function onPositionChanged(ms) {
            if (ms > 0 && !hook.playing) {
                hook.playing = true
                hook.post({ playing: true, position_ms: ms })
            }
        }

        function onPlaybackEnded(finalPositionMs, finalDurationMs, reason) {
            hook.playing = false
            hook.post({ playing: false })
        }
    }

    // positionChanged arrives as often as mpv reports time-pos, which is far
    // more than a display showing whole seconds can use. One post a second.
    Timer {
        interval: 1000
        repeat: true
        running: hook.playing
        onTriggered: hook.post({ playing: true, position_ms: mpvController.position })
    }
}

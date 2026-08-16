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

    function setView(url) {
        post({ view: relativeView(url) })
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

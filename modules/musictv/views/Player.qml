import QtQuick

// Continuous music-video playback, in the shape a 1980s music-video channel
// broadcast in.
//
// The whole rotation is handed to mpv as ONE playlist: bumper, 2-4 music
// videos, bumper, ... Bare URLs appended after the first one on mpv's command
// line are how mpv builds a playlist, and it resolves each through yt-dlp
// lazily as it reaches it. Deliberately NOT one mpv launch per video: on the Pi
// every launch switches VT and re-negotiates DRM, which would put a black hole
// between every video.
//
// The on-screen furniture (title card, screen bug) cannot be drawn from here —
// mpv owns the screen once it starts. It is drawn by scripts/musictv-overlay.lua,
// which this view loads into mpv via extraArgs and configures with
// --script-opts-append.
FocusScope {
    id: playerRoot

    property var navParams: ({})

    signal goBack()

    // Rotation
    property var  music:    []      // pools from musictvBackend
    property var  bumpers:  []
    property var  queue:    []      // the interleaved playlist handed to mpv
    property int  queuePos: 0

    property bool playbackStarted: false
    property string statusMessage: "TUNING IN..."
    property string errorMessage:  ""

    // Enough for a long sitting; on eof the queue is rebuilt and mpv relaunched,
    // so this is a batch size, not a session limit.
    readonly property int queueTarget: 120

    focus: true

    // ---
    // Paths
    // ---

    // 240-MP's dataRoot, from the backend so the overlay script reads the same
    // cache the backend writes.
    function _dataRoot() {
        return musictvBackend.data_root()
    }

    // Filesystem path of this module's directory (…/modules/musictv), derived from
    // this QML file's own URL — the module can be installed under any appRoot.
    function _moduleDir() {
        var u = Qt.resolvedUrl(".").toString()          // file:///…/modules/musictv/views/
        u = u.replace(/^file:\/\//, "").replace(/\/+$/, "")
        return u.replace(/\/views$/, "")
    }

    // ---
    // Queue building
    // ---

    function shuffled(list) {
        var a = list.slice()
        for (var i = a.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1))
            var t = a[i]; a[i] = a[j]; a[j] = t
        }
        return a
    }

    // How many music videos to play before the next bumper. "2-4" (the default)
    // re-rolls per segment; an explicit number is fixed.
    function segmentLength() {
        var setting = appCore.get_setting(moduleRoot.moduleId, "videos_per_break") || "2-4"
        var n = parseInt(setting)
        if (!isNaN(n) && n > 0)
            return n
        return 2 + Math.floor(Math.random() * 3)   // 2, 3 or 4
    }

    // Bumper, then a segment of music videos, repeated. Each pool is drawn from
    // in shuffled order and only reshuffled once exhausted, so nothing repeats
    // until everything else has played — a plain random pick would replay the
    // same bumper twice in a row on a short bumper list.
    function buildQueue() {
        var q = []
        if (music.length === 0)
            return q

        var m = shuffled(music),  mi = 0
        var b = shuffled(bumpers), bi = 0

        while (q.length < queueTarget) {
            if (b.length > 0) {
                if (bi >= b.length) { b = shuffled(bumpers); bi = 0 }
                q.push(b[bi++])
            }
            var n = segmentLength()
            for (var i = 0; i < n && q.length < queueTarget; i++) {
                if (mi >= m.length) { m = shuffled(music); mi = 0 }
                q.push(m[mi++])
            }
        }
        return q
    }

    // ---
    // Playback
    // ---

    function ytdlFormat() {
        var res = appCore.get_setting(moduleRoot.moduleId, "playback_resolution") || "480p"
        var height = res === "1080p" ? 1080 : (res === "720p" ? 720 : 480)
        // H.264 first (Pi hardware decode), then any codec at the cap, then best.
        return "bestvideo[height<=?" + height + "][vcodec^=avc1]+bestaudio/" +
               "bestvideo[height<=?" + height + "]+bestaudio/" +
               "best[height<=?" + height + "]/best"
    }

    function settingOn(key) {
        var raw = appCore.get_setting(moduleRoot.moduleId, key)
        // Unset means "use the manifest default", which is ON for both overlays.
        return (raw === undefined || raw === null || raw === "") ? true
               : (raw === true || raw === "ON")
    }

    function overlayArgs() {
        var dir = _moduleDir()
        var args = [
            "--script=" + dir + "/scripts/musictv-overlay.lua",
            "--script-opts-append=musictv-cache=" + _dataRoot() + "/musictv_cache.json",
            "--script-opts-append=musictv-bugs=" + _dataRoot() + "/musictv_bugs",
            "--script-opts-append=musictv-titles=" + (settingOn("show_titles") ? "1" : "0"),
            "--script-opts-append=musictv-bug=" + (settingOn("show_screenbug") ? "1" : "0"),
            "--script-opts-append=musictv-font=" + titleFont()
        ]
        // No --osd-fonts-dir: MpvController appends its own (for the OSC font)
        // after extraArgs and mpv keeps only the last one, so the title font has
        // to be installed in the user font directory instead. See README.
        return args
    }

    // Family name handed to the overlay. Kabel is not shipped (commercial
    // licence); choosing it assumes the user has installed their own copy.
    function titleFont() {
        var font = appCore.get_setting(moduleRoot.moduleId, "title_font") || "Josefin Sans"
        return font === "Kabel" ? "Kabel" : "Josefin Sans SemiBold"
    }

    function startPlayback() {
        queue = buildQueue()
        if (queue.length === 0) {
            errorMessage = "NO MUSIC VIDEOS FOUND\n\n" +
                           "ADD PLAYLISTS TO musictv_playlists.txt"
            return
        }
        queuePos = 0
        statusMessage = "TUNING IN..."

        var extra = ["--ytdl=yes", "--ytdl-format=" + ytdlFormat()].concat(overlayArgs())
        // Playlist entries go last: everything after them is still parsed as a
        // global option, but keeping the order flags-then-URLs matches how mpv's
        // own docs describe a playlist command line.
        for (var i = 1; i < queue.length; i++)
            extra.push(queue[i].url)

        console.log("[MusicTV] playing " + queue.length + " videos (" +
                    music.length + " music, " + bumpers.length + " bumpers)")
        mpvController.loadAndPlay(queue[0].url, 0, 0, -2, [], [], false, -1, 0.0,
                                  "", false, "", false, [], 0.0, false, extra)
    }

    // Starting mpv runs synchronously and, on the Pi, immediately switches VT
    // (suspending Qt's render thread) before the status frame can paint. Defer
    // the launch one tick so the "TUNING IN" text is rendered first.
    Timer {
        id: startTimer
        interval: 50
        repeat: false
        onTriggered: startPlayback()
    }

    // ---
    // Pools
    // ---

    // musictvBackend lists the playlists with yt-dlp — only when an input file
    // changed or the cache is a day old, otherwise straight from the cache — and
    // answers with poolsLoaded or errorOccurred.
    function loadPools() {
        errorMessage = ""
        statusMessage = "TUNING IN..."
        musictvBackend.load()
    }

    Component.onCompleted: loadPools()

    Connections {
        target: musictvBackend

        function onRefreshStarted() {
            playerRoot.statusMessage = "UPDATING PLAYLISTS..."
        }

        function onPoolsLoaded(music, bumpers) {
            playerRoot.music   = music
            playerRoot.bumpers = bumpers
            console.log("[MusicTV] pools: " + music.length + " music, " + bumpers.length + " bumpers")
            startTimer.restart()
        }

        function onErrorOccurred(message) {
            playerRoot.errorMessage = message
        }
    }

    // ---
    // mpv
    // ---

    Connections {
        target: mpvController

        function onPositionChanged(ms) {
            if (ms > 0) playerRoot.playbackStarted = true
        }

        function onPlaylistPosChanged(pos) {
            if (pos >= 0) playerRoot.queuePos = pos
        }

        function onPlaybackEnded(finalPositionMs, finalDurationMs, reason) {
            if (reason === "failed" && !playerRoot.playbackStarted) {
                playerRoot.errorMessage = "PLAYBACK FAILED\n\n" +
                                          "PLEASE CHECK THAT YT-DLP IS INSTALLED AND UP TO DATE"
                return
            }
            // The rotation only ends when the viewer ends it. Reaching the end of
            // the batch just means it is time for another one.
            if (reason === "eof") {
                playerRoot.playbackStarted = false
                startTimer.restart()
                return
            }
            playerRoot.goBack()
        }
    }

    Keys.onPressed: function(event) {
        if (errorMessage !== "") {
            if (event.key === Qt.Key_Escape || event.key === Qt.Key_Backspace || event.key === Qt.Key_Back) {
                goBack()
                event.accepted = true
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                loadPools()
                event.accepted = true
            }
        } else if (event.key === Qt.Key_Escape || event.key === Qt.Key_Back) {
            mpvController.sendKey("ESC")
            event.accepted = true
        } else if (event.key === Qt.Key_Backspace) {
            mpvController.sendKey("BS")
            event.accepted = true
        } else if (event.key === Qt.Key_Up) {
            mpvController.sendKey("UP")
            event.accepted = true
        } else if (event.key === Qt.Key_Down) {
            mpvController.sendKey("DOWN")
            event.accepted = true
        } else if (event.key === Qt.Key_Left) {
            mpvController.sendKey("LEFT")
            event.accepted = true
        } else if (event.key === Qt.Key_Right) {
            mpvController.sendKey("RIGHT")
            event.accepted = true
        } else if (event.key === Qt.Key_Space) {
            mpvController.sendKey("SPACE")
            event.accepted = true
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            mpvController.sendKey("ENTER")
            event.accepted = true
        }
    }

    // ---
    // UI — only ever visible before mpv takes the screen, or on error
    // ---

    Rectangle {
        anchors.fill: parent
        color: "black"

        Text {
            text: statusMessage
            color: "white"
            font.family: root.globalFont
            anchors.centerIn: parent
            font.pixelSize: root.sh * 0.05
            visible: !playbackStarted && errorMessage === ""
        }

        Column {
            anchors.centerIn: parent
            spacing: root.sh * 0.05
            visible: errorMessage !== ""

            Text {
                text: errorMessage
                color: "white"
                font.family: root.globalFont
                width: root.sw * 0.76875
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                anchors.horizontalCenter: parent.horizontalCenter
                font.pixelSize: root.sh * 0.0375
            }
            Text {
                text: root.hints.back + ":BACK " + root.hints.select + ":RETRY"
                color: "#919191"
                font.family: root.globalFont
                anchors.horizontalCenter: parent.horizontalCenter
                font.pixelSize: root.sh * 0.0333333
            }
        }
    }
}

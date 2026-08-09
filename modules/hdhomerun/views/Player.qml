import QtQuick

// HDHomeRun live player. A simplified sibling of modules/plex/views/LivePlayer.qml.
//
// Two things Plex needs and this deliberately does not:
//   - no sessionId / stop_live_session: an HDHomeRun tuner is held by the open HTTP
//     connection to the stream URL itself, and released when mpv closes it.
//   - no keep-alive timer: there is no lease to renew, so nothing to ping.
//
// The stream is raw MPEG-TS straight off the tuner (not HLS), so there is no
// transcode offset and no token to pass to mpv.
FocusScope {
    id: playerRoot

    property var navParams: ({})

    // Injected by Root.qml — see HdhrService.qml.
    property var hdhr

    signal navigateTo(string path, var params)
    signal goBack()

    // The channel to watch: { guideNumber, guideName, streamUrl }.
    property var channel: navParams.channel || ({})

    property string streamUrl: ""
    property bool playbackStarted: false
    property bool exiting: false

    focus: true

    // tune_channel resolves through onStreamUrlReady, which loads the stream
    // into mpv. It resolves from the already-loaded lineup, so this is fast.
    function tune() {
        if (!channel.guideNumber) { goBack(); return }
        playbackStarted = false
        hdhr.tune_channel(channel.guideNumber)
    }

    function teardown() {
        if (exiting) return
        exiting = true
        mpvController.stop()
    }

    Component.onCompleted: tune()

    Connections {
        target: hdhr

        function onStreamUrlReady(url) {
            playerRoot.streamUrl = url
            // Live TS: start at 0, default audio track, no subtitles.
            mpvController.loadAndPlay(url, 0, 0, -1)
        }

        function onErrorOccurred(msg) {
            console.log("[HDHomeRun/Player] Error: " + msg)
            teardown()
            goBack()
        }
    }

    Connections {
        target: mpvController

        function onPlaybackEnded(finalPositionMs, finalDurationMs, reason) {
            // Any end (user quit, stream failure, eof) returns to the channel list.
            teardown()
            goBack()
        }
    }

    // Forward keys to mpv so its OSC works on the Pi, where the Qt app owns the
    // keyboard and relays via sendKey. On desktop mpv has focus and handles these
    // itself. Same set LivePlayer.qml forwards.
    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape || event.key === Qt.Key_Back) {
            // Quit mpv; onPlaybackEnded drives the teardown + goBack.
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
        }
    }

    // Tuning indicator — shown until mpv has the stream up and covering us.
    Text {
        visible: !playerRoot.playbackStarted
        text: playerRoot.channel.guideNumber
              ? "TUNING " + playerRoot.channel.guideNumber + "..."
              : "TUNING..."
        color: root.tertiaryColor
        font.family: root.globalFont
        font.capitalization: Font.AllUppercase
        anchors.centerIn: parent
        font.pixelSize: root.sh * 0.05 //24
    }
}

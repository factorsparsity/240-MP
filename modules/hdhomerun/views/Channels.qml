import QtQuick
import Components

// HDHomeRun channel list. Mirrors modules/plex/views/LiveChannels.qml, but the
// model comes straight from the tuner's /lineup.json — each entry is
// { guideNumber, guideName, streamUrl } as built by HDHomeRunBackend::load_channels().
FocusScope {
    id: channelsRoot

    property var navParams: ({})
    property var navListState: navParams.navListState || ({})

    // Injected by Root.qml. Replaces the old hdhomerunBackend context property,
    // which only existed because the backend was compiled into the binary.
    property var hdhr

    signal navigateTo(string path, var params, var listState)
    signal goBack()

    property var channels: []
    property bool loaded: false
    property string errorMessage: ""

    // Guards the auto-open below so returning from the picker without choosing
    // anything doesn't bounce straight back into it.
    property bool _autoOpened: false

    Connections {
        target: hdhr

        function onChannelsLoaded(items) {
            channelsRoot.channels = items
            channelsRoot.loaded = true
            if (items.length > 0) {
                var restore = (navListState.currentIndex !== undefined) ? navListState.currentIndex : 0
                channelList.currentIndex = Math.min(restore, items.length - 1)
                channelList.positionViewAtIndex(channelList.currentIndex, ListView.Contain)
            }
        }

        function onErrorOccurred(msg) {
            console.log("[HDHomeRun/Channels] Error: " + msg)
            channelsRoot.loaded = true
            channelsRoot.errorMessage = msg

            // No tuner configured is not an error the user can act on from here —
            // it means they have never picked one. Open the picker instead of
            // showing a dead end whose only escape is an undiscoverable keypress.
            // callLater because this can fire during Component.onCompleted, and
            // navigating while the Loader is still setting this view up loses it.
            if (msg.indexOf("NO HDHOMERUN DEVICE") === 0 && !channelsRoot._autoOpened) {
                channelsRoot._autoOpened = true
                Qt.callLater(function() {
                    channelsRoot.navigateTo("Devices.qml", {}, {})
                })
            }
        }
    }

    Component.onCompleted: hdhr.load_channels()

    focus: true

    // ---
    // UI
    // ---

    // Header
    AppBar {
        iconSource: moduleRoot.moduleIcon
        title: moduleRoot.moduleName
        subtitle: "LIVE TV"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.125 //60
        anchors.leftMargin: root.sw * 0.125 //80
    }

    // Which tuner is in use. On screen permanently rather than tucked behind the
    // D shortcut: this is the module's most important piece of configuration and
    // it has no row in the app's settings screen (a dynamic options list needs a
    // C++ backend, which this module deliberately does not have).
    Text {
        text: (hdhr && hdhr.deviceIp() !== "") ? "TUNER " + hdhr.deviceIp() + "   D:CHANGE"
                                               : "NO TUNER SET   D:CHOOSE"
        color: root.tertiaryColor
        font.family: root.globalFont
        font.capitalization: Font.AllUppercase
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.1958333
        anchors.leftMargin: root.sw * 0.115625
        font.pixelSize: root.sh * 0.0333333
    }

    // Loading / empty / error indicator
    Text {
        visible: channels.length === 0
        text: channelsRoot.errorMessage !== "" ? channelsRoot.errorMessage
              : (channelsRoot.loaded ? "NO CHANNELS" : "LOADING...")
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.centerIn: parent
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        width: root.sw * 0.6
        font.pixelSize: root.sh * 0.05 //24
    }

    // Body
    ListView {
        id: channelList
        model: channels
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.25 //120
        anchors.leftMargin: root.sw * 0.115625 //74
        width: root.sw * 0.76875 //492
        height: root.sh * 0.525 //252
        clip: true
        focus: true

        Keys.onUpPressed: {
            if (count === 0) return
            if (currentIndex > 0) currentIndex--
            else currentIndex = count - 1
            channelList.positionViewAtIndex(channelList.currentIndex, ListView.Contain)
        }
        Keys.onDownPressed: {
            if (count === 0) return
            if (currentIndex < count - 1) currentIndex++
            else currentIndex = 0
            channelList.positionViewAtIndex(channelList.currentIndex, ListView.Contain)
        }

        Keys.onReturnPressed: {
            if (channels.length === 0) return
            channelsRoot.navigateTo("Player.qml", {
                channel: channels[currentIndex]
            }, { currentIndex: channelList.currentIndex })
        }

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Escape || event.key === Qt.Key_Backspace || event.key === Qt.Key_Back) {
                channelsRoot.goBack()
                event.accepted = true
            } else if (event.key === Qt.Key_Right) {
                // Right opens the EPG grid; Left/Right are otherwise unused in a
                // vertical list, so this costs no existing navigation.
                channelsRoot.navigateTo("Guide.qml", {}, { currentIndex: channelList.currentIndex })
                event.accepted = true
            } else if (event.key === Qt.Key_D) {
                // Tuner selection lives here rather than in the settings screen:
                // without a C++ backend a manifest setting cannot be populated
                // dynamically. See Devices.qml.
                channelsRoot.navigateTo("Devices.qml", {}, { currentIndex: channelList.currentIndex })
                event.accepted = true
            }
        }

        delegate: Item {
            width: channelList.width
            height: root.sh * 0.0583333 //28

            Item {
                id: textClip
                width: Math.min(rowText.implicitWidth, channelList.width)
                height: parent.height
                clip: true

                Rectangle {
                    color: root.accentColor
                    anchors.fill: rowText
                    visible: channelList.currentIndex === index
                }

                Text {
                    id: rowText
                    text: (modelData.guideNumber ? modelData.guideNumber + "  " : "") + (modelData.guideName || "")
                    color: channelList.currentIndex === index ? root.surfaceColor : root.primaryColor
                    font.family: root.globalFont
                    font.capitalization: Font.AllUppercase
                    anchors.verticalCenter: parent.verticalCenter
                    x: 0
                    topPadding: root.sh * 0.0041667 //2
                    leftPadding: root.sw * 0.009375 //6
                    rightPadding: root.sw * 0.009375 //6
                    bottomPadding: root.sh * 0.00625 //3
                    font.pixelSize: root.sh * 0.05 //24
                }

                SequentialAnimation {
                    running: (channelList.currentIndex === index) &&
                             (rowText.implicitWidth > textClip.width)
                    loops: Animation.Infinite
                    onRunningChanged: if (!running) rowText.x = 0
                    PauseAnimation { duration: 1500 }
                    NumberAnimation {
                        target: rowText; property: "x"
                        to: textClip.width - rowText.implicitWidth
                        duration: Math.abs(to) * 20
                    }
                    PauseAnimation { duration: 2000 }
                    PropertyAction { target: rowText; property: "x"; value: 0 }
                }
            }
        }
    }

    // Footer
    Text {
        id: footer
        text: root.hints.back + ":BACK " + root.hints.navigate + ":NAVIGATE " +
              root.hints.select + ":WATCH  >:GUIDE  D:TUNER"
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.bottomMargin: root.sh * 0.1041667 //50
        anchors.leftMargin: root.sw * 0.125 //80
        font.pixelSize: root.sh * 0.0333333 //16
    }
}

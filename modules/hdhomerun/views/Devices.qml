import QtQuick
import Components

// Tuner picker.
//
// This screen exists because the module has no C++ backend any more. Device
// selection used to be a manifest `list_single` with options_source "dynamic",
// which requires an options_slot on a backend — so with the backend gone the
// setting could not be populated and had to leave the settings screen entirely.
// appCore.save_setting() is global, so persisting the choice still works fine.
//
// Deliberately no free-text IP entry: 240-MP has no text-input setting type and
// building a keyboard here is out of proportion. A tuner that discovery cannot
// see (no internet — see HdhrService.discoverDevices) is set by seeding
// device_id in config.json instead.
FocusScope {
    id: devicesRoot

    property var navParams: ({})
    property var hdhr

    signal navigateTo(string path, var params, var listState)
    signal goBack()

    property var devices: []
    property bool loaded: false
    property string errorMessage: ""
    property string selectedIp: hdhr ? hdhr.deviceIp() : ""

    focus: true

    Connections {
        target: hdhr

        function onDevicesDiscovered(items) {
            devicesRoot.devices = items
            devicesRoot.loaded = true
            for (var i = 0; i < items.length; i++) {
                if (items[i].ip === devicesRoot.selectedIp) {
                    deviceList.currentIndex = i
                    break
                }
            }
        }

        function onErrorOccurred(msg) {
            devicesRoot.loaded = true
            devicesRoot.errorMessage = msg
        }
    }

    Component.onCompleted: hdhr.discoverDevices()

    AppBar {
        iconSource: moduleRoot.moduleIcon
        title: moduleRoot.moduleName
        subtitle: "SELECT TUNER"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.125
        anchors.leftMargin: root.sw * 0.125
    }

    Text {
        visible: devices.length === 0
        // Discovery goes over the internet (QML has no UDP broadcast), so the
        // failure mode users will actually hit is "no network", not "no tuner".
        // Say so, and name the offline escape hatch rather than dead-ending.
        text: !devicesRoot.loaded ? "SEARCHING..."
              : (devicesRoot.errorMessage !== ""
                 ? devicesRoot.errorMessage + "\n\nDISCOVERY NEEDS INTERNET.\nSET device_id IN config.json TO USE A TUNER OFFLINE."
                 : "NO TUNERS FOUND\n\nCHECK THE TUNER IS POWERED AND ON THIS NETWORK.")
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.centerIn: parent
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        width: root.sw * 0.6
        font.pixelSize: root.sh * 0.05
    }

    ListView {
        id: deviceList
        model: devices
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.25
        anchors.leftMargin: root.sw * 0.115625
        width: root.sw * 0.76875
        height: root.sh * 0.525
        clip: true
        focus: true

        Keys.onUpPressed: {
            if (count === 0) return
            currentIndex = (currentIndex > 0) ? currentIndex - 1 : count - 1
            positionViewAtIndex(currentIndex, ListView.Contain)
        }
        Keys.onDownPressed: {
            if (count === 0) return
            currentIndex = (currentIndex < count - 1) ? currentIndex + 1 : 0
            positionViewAtIndex(currentIndex, ListView.Contain)
        }

        Keys.onReturnPressed: {
            if (devices.length === 0) return
            // Persists device_id and reloads the lineup against it. Going back
            // lands on Channels.qml, which re-runs load_channels() on completion.
            hdhr.selectDevice(devices[currentIndex].ip)
            devicesRoot.goBack()
        }

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Escape || event.key === Qt.Key_Backspace || event.key === Qt.Key_Back) {
                devicesRoot.goBack()
                event.accepted = true
            }
        }

        delegate: Item {
            width: deviceList.width
            height: root.sh * 0.0583333

            Item {
                width: Math.min(rowText.implicitWidth, deviceList.width)
                height: parent.height
                clip: true

                Rectangle {
                    color: root.accentColor
                    anchors.fill: rowText
                    visible: deviceList.currentIndex === index
                }

                Text {
                    id: rowText
                    // A leading marker rather than a separate column: the label is
                    // already long and the list elides at the right edge.
                    text: (modelData.ip === devicesRoot.selectedIp ? "> " : "  ") + modelData.label
                    color: deviceList.currentIndex === index ? root.surfaceColor : root.primaryColor
                    font.family: root.globalFont
                    font.capitalization: Font.AllUppercase
                    anchors.verticalCenter: parent.verticalCenter
                    topPadding: root.sh * 0.0041667
                    leftPadding: root.sw * 0.009375
                    rightPadding: root.sw * 0.009375
                    bottomPadding: root.sh * 0.00625
                    font.pixelSize: root.sh * 0.05
                }
            }
        }
    }

    Text {
        text: root.hints.back + ":BACK " + root.hints.navigate + ":NAVIGATE " +
              root.hints.select + ":USE TUNER"
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.bottomMargin: root.sh * 0.1041667
        anchors.leftMargin: root.sw * 0.125
        font.pixelSize: root.sh * 0.0333333
    }
}

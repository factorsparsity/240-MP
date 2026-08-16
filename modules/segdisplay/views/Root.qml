import QtQuick
import Components

// Front Panel status screen.
//
// The module exists mainly so its settings land in the normal place
// (Settings > Modules > Front Panel). This view is the bring-up tool: it says
// whether the daemon is up, whether the I2C device answered, and what the panel
// is showing right now — the three things you cannot see from a display buried
// in a cable box. T runs the display test.
FocusScope {
    id: moduleRoot

    signal goBack()

    property var navParams: ({})

    property string moduleId: "com.240mp.segdisplay"

    property var _moduleInfo: appCore ? appCore.get_module_info(moduleId) : ({})
    property string moduleName: _moduleInfo.name || ""
    property string moduleIcon: _moduleInfo.icon || ""

    // Must match SEGDISPLAY_PORT in segdisplay.py.
    readonly property string baseUrl: "http://127.0.0.1:8377"

    property bool daemonUp: false
    property bool deviceUp: false
    property string panelText: ""
    property string lastError: ""

    focus: true

    function refresh() {
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE)
                return
            if (xhr.status !== 200) {
                moduleRoot.daemonUp = false
                moduleRoot.deviceUp = false
                moduleRoot.panelText = ""
                moduleRoot.lastError = ""
                return
            }
            var d = {}
            try {
                d = JSON.parse(xhr.responseText)
            } catch (e) {
                return
            }
            moduleRoot.daemonUp = true
            moduleRoot.deviceUp = !!d.connected
            moduleRoot.panelText = d.text || ""
            moduleRoot.lastError = d.error || ""
        }
        xhr.open("GET", moduleRoot.baseUrl + "/status")
        xhr.send()
    }

    function runTest() {
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {}
        xhr.open("POST", moduleRoot.baseUrl + "/test")
        xhr.send("{}")
    }

    Component.onCompleted: refresh()

    Timer {
        interval: 1000
        repeat: true
        running: true
        onTriggered: moduleRoot.refresh()
    }

    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape || event.key === Qt.Key_Backspace || event.key === Qt.Key_Back) {
            moduleRoot.goBack()
            event.accepted = true
        } else if (event.key === Qt.Key_T) {
            moduleRoot.runTest()
            event.accepted = true
        }
    }

    AppBar {
        iconSource: moduleRoot.moduleIcon
        title: moduleRoot.moduleName
        subtitle: "OLED FRONT PANEL"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.125
        anchors.leftMargin: root.sw * 0.125
    }

    Column {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.28
        anchors.leftMargin: root.sw * 0.125
        spacing: root.sh * 0.03

        Text {
            text: moduleRoot.daemonUp ? "DAEMON: RUNNING" : "DAEMON: NOT RESPONDING"
            color: moduleRoot.daemonUp ? root.primaryColor : root.tertiaryColor
            font.family: root.globalFont
            font.pixelSize: root.sh * 0.05
        }

        Text {
            text: moduleRoot.deviceUp ? "DISPLAY: CONNECTED" : "DISPLAY: NOT FOUND"
            color: moduleRoot.deviceUp ? root.primaryColor : root.tertiaryColor
            font.family: root.globalFont
            font.pixelSize: root.sh * 0.05
        }

        Text {
            text: "SHOWING: " + (moduleRoot.panelText === "" ? "(BLANK)" : moduleRoot.panelText)
            color: root.primaryColor
            font.family: root.globalFont
            font.pixelSize: root.sh * 0.05
        }

        Text {
            visible: !moduleRoot.deviceUp && moduleRoot.lastError !== ""
            text: moduleRoot.lastError.toUpperCase()
            color: root.tertiaryColor
            font.family: root.globalFont
            wrapMode: Text.WordWrap
            width: root.sw * 0.75
            font.pixelSize: root.sh * 0.0333333
        }
    }

    Text {
        text: root.hints.back + ":BACK  T:DISPLAY TEST"
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.bottomMargin: root.sh * 0.1041667
        anchors.leftMargin: root.sw * 0.125
        font.pixelSize: root.sh * 0.0333333
    }
}

import QtQuick
import Components

// Retro EPG grid: channels down the left, a scrolling time window across the top,
// programme blocks sized in proportion to their duration — the classic cable-box
// guide layout.
//
// Data comes from two independent backend calls: load_channels() for the lineup
// (which channels exist, and their stream URLs) and load_guide() for the PSIP
// events that epg_scanner.py cached. The guide is keyed by GuideNumber, so the
// two join on that.
//
// Navigation: Up/Down changes channel, Left/Right steps through that channel's
// programmes (scrolling the time window to follow the selection), Enter tunes
// the selected channel, Back returns to the channel list.
FocusScope {
    id: guideRoot

    property var navParams: ({})
    // Set by Root.navigateBack() from the listState this view pushed on its way
    // out, so coming back from a channel lands on that channel rather than at
    // the top of the lineup. Same contract Channels.qml uses.
    property var navListState: navParams.navListState || ({})

    // Injected by Root.qml — see HdhrService.qml.
    property var hdhr

    signal navigateTo(string path, var params, var listState)
    signal goBack()

    property var channels: []
    property var guide: ({})
    property bool channelsReady: false
    property bool guideReady: false

    // Time window shown across the grid.
    property date windowStart: floorToHalfHour(new Date())
    property int windowMinutes: 90

    property int currentChannel: 0
    property int currentEvent: 0

    // Wall clock, re-read on a timer so both the header clock and the "now"
    // marker stay live while the guide sits open.
    property date now: new Date()
    Timer {
        interval: 15000
        running: true
        repeat: true
        onTriggered: {
            guideRoot.now = new Date()
            // A ticking clock can make the window illegal (it is pinned to the
            // current half-hour), so re-clamp whenever time moves.
            if (guideRoot.windowStart < guideRoot.earliestWindowStart())
                guideRoot.windowStart = guideRoot.earliestWindowStart()
        }
    }

    focus: true

    // ---
    // Time helpers
    // ---

    function floorToHalfHour(d) {
        var out = new Date(d)
        out.setMinutes(out.getMinutes() < 30 ? 0 : 30, 0, 0)
        return out
    }

    function shiftMinutes(d, mins) {
        var out = new Date(d)
        out.setMinutes(out.getMinutes() + mins)
        return out
    }

    // Classic guide formatting: 7:00P, 12:30A.
    function formatTime(d) {
        var h = d.getHours()
        var suffix = h < 12 ? "A" : "P"
        var h12 = h % 12
        if (h12 === 0) h12 = 12
        var m = d.getMinutes()
        return h12 + ":" + (m < 10 ? "0" + m : m) + suffix
    }

    function windowEnd() { return shiftMinutes(windowStart, windowMinutes) }

    // Retro guide date line, e.g. "SUN JUL 26".
    function formatDate(d) {
        var days = ["SUN","MON","TUE","WED","THU","FRI","SAT"]
        var months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
        return days[d.getDay()] + " " + months[d.getMonth()] + " " + d.getDate()
    }

    // The guide never scrolls into the past — a programme that already finished
    // is not something you can tune. Everything clamps to the current half-hour.
    function earliestWindowStart() { return floorToHalfHour(now) }

    // Index of the programme airing right now; Left navigation stops here.
    function currentAiringIndex(guideNumber) { return eventIndexAt(guideNumber, now) }

    // ---
    // Data helpers
    // ---

    function eventsFor(guideNumber) {
        var list = guide[guideNumber]
        return list === undefined ? [] : list
    }

    // Whether anything at all lands inside the visible time window. A channel can
    // have hundreds of cached events and still show an empty row, because the
    // cache went stale and every one of them ended hours ago — which is not the
    // same thing as having no data, and the row says so either way.
    function hasEventsInWindow(guideNumber) {
        var evs = eventsFor(guideNumber)
        var start = windowStart.getTime()
        var end = start + windowMinutes * 60000
        for (var i = 0; i < evs.length; i++) {
            if (new Date(evs[i].end).getTime() > start
                    && new Date(evs[i].start).getTime() < end)
                return true
        }
        return false
    }

    function currentChannelNumber() {
        if (currentChannel < 0 || currentChannel >= channels.length) return ""
        return channels[currentChannel].guideNumber
    }

    function selectedEvent() {
        var evs = eventsFor(currentChannelNumber())
        if (currentEvent < 0 || currentEvent >= evs.length) return null
        return evs[currentEvent]
    }

    // Pick the programme airing at windowStart, so changing channel keeps the
    // selection at roughly the same point in time rather than jumping to 00:00.
    function eventIndexAt(guideNumber, when) {
        var evs = eventsFor(guideNumber)
        for (var i = 0; i < evs.length; i++) {
            if (new Date(evs[i].end) > when) return i
        }
        return evs.length > 0 ? evs.length - 1 : 0
    }

    // Keep the time window following the selected programme, never scrolling
    // earlier than the current half-hour.
    function scrollToSelection() {
        var ev = selectedEvent()
        if (!ev) return
        var start = new Date(ev.start)
        if (start < windowStart || start >= windowEnd())
            windowStart = floorToHalfHour(start)
        if (windowStart < earliestWindowStart())
            windowStart = earliestWindowStart()
    }

    Connections {
        target: hdhr

        function onChannelsLoaded(items) {
            guideRoot.channels = items
            guideRoot.channelsReady = true
            if (items.length > 0) {
                var restore = (guideRoot.navListState.currentIndex !== undefined)
                            ? guideRoot.navListState.currentIndex : 0
                guideRoot.currentChannel = Math.min(restore, items.length - 1)
                // forceLayout first: the model was assigned a line ago, so the
                // delegates positionViewAtIndex needs do not exist yet and the
                // scroll would silently do nothing.
                channelColumn.forceLayout()
                channelColumn.positionViewAtIndex(guideRoot.currentChannel, ListView.Contain)
            }
            guideRoot.currentEvent = guideRoot.eventIndexAt(guideRoot.currentChannelNumber(),
                                                           guideRoot.windowStart)
        }

        function onGuideLoaded(g) {
            guideRoot.guide = g
            guideRoot.guideReady = true
            guideRoot.currentEvent = guideRoot.eventIndexAt(guideRoot.currentChannelNumber(),
                                                           guideRoot.windowStart)
        }

        function onErrorOccurred(msg) {
            console.log("[HDHomeRun/Guide] Error: " + msg)
        }
    }

    Component.onCompleted: {
        hdhr.load_channels()
        hdhr.load_guide()
    }

    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape || event.key === Qt.Key_Backspace || event.key === Qt.Key_Back) {
            guideRoot.goBack()
            event.accepted = true
        } else if (event.key === Qt.Key_Up) {
            if (channels.length > 0) {
                currentChannel = (currentChannel > 0) ? currentChannel - 1 : channels.length - 1
                currentEvent = eventIndexAt(currentChannelNumber(), windowStart)
                channelColumn.positionViewAtIndex(currentChannel, ListView.Contain)
            }
            event.accepted = true
        } else if (event.key === Qt.Key_Down) {
            if (channels.length > 0) {
                currentChannel = (currentChannel < channels.length - 1) ? currentChannel + 1 : 0
                currentEvent = eventIndexAt(currentChannelNumber(), windowStart)
                channelColumn.positionViewAtIndex(currentChannel, ListView.Contain)
            }
            event.accepted = true
        } else if (event.key === Qt.Key_Left) {
            // Stop at whatever is airing now rather than walking into the past.
            if (currentEvent > currentAiringIndex(currentChannelNumber())) {
                currentEvent--; scrollToSelection()
            }
            event.accepted = true
        } else if (event.key === Qt.Key_Right) {
            if (currentEvent < eventsFor(currentChannelNumber()).length - 1) {
                currentEvent++; scrollToSelection()
            }
            event.accepted = true
        } else if (event.key === Qt.Key_C) {
            // Left/Right are the time axis here, so the channel list needs a key
            // of its own — without one it is unreachable when start_view is
            // Guide (Back exits the module from the first view).
            guideRoot.navigateTo("Channels.qml", {}, { currentIndex: currentChannel })
            event.accepted = true
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (channels.length > 0) {
                guideRoot.navigateTo("Player.qml", { channel: channels[currentChannel] },
                                     { currentIndex: currentChannel })
            }
            event.accepted = true
        }
    }

    // ---
    // UI
    // ---

    AppBar {
        id: appBar
        iconSource: moduleRoot.moduleIcon
        title: moduleRoot.moduleName
        subtitle: "GUIDE"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.125
        anchors.leftMargin: root.sw * 0.125
    }

    // Live clock, top-right alongside the title — the label column is only 28%
    // of the grid, too narrow to hold "SUN JUL 26 3:37P" without eliding it.
    Text {
        id: clock
        anchors.right: parent.right
        anchors.rightMargin: root.sw * 0.125
        anchors.verticalCenter: appBar.verticalCenter
        text: guideRoot.formatDate(guideRoot.now) + "  " + guideRoot.formatTime(guideRoot.now)
        color: root.accentColor
        font.family: root.globalFont
        font.capitalization: Font.AllUppercase
        font.pixelSize: root.sh * 0.0375
    }

    // Empty / loading state
    Text {
        visible: channels.length === 0 || Object.keys(guide).length === 0
        text: !guideRoot.channelsReady ? "LOADING..."
              : (Object.keys(guide).length === 0 ? "NO GUIDE DATA - RUN RESCAN GUIDE DATA IN SETTINGS"
                                                 : "NO CHANNELS")
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.centerIn: parent
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        width: root.sw * 0.6
        font.pixelSize: root.sh * 0.05
    }

    Item {
        id: grid
        visible: channels.length > 0 && Object.keys(guide).length > 0
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: root.sh * 0.19
        anchors.leftMargin: root.sw * 0.115625
        width: root.sw * 0.76875
        // Bottom is anchored to the detail pane rather than a fixed height:
        // a fixed height overlapped the detail text, since the grid ran to
        // ~0.70 of the screen while the detail pane started around ~0.68.
        anchors.bottom: detail.top
        anchors.bottomMargin: root.sh * 0.02

        property real labelWidth: width * 0.28
        property real laneWidth: width - labelWidth
        property real pxPerMinute: laneWidth / guideRoot.windowMinutes
        property real rowHeight: root.sh * 0.0666667

        // Time header
        Row {
            id: timeHeader
            x: grid.labelWidth
            height: grid.rowHeight
            Repeater {
                model: Math.floor(guideRoot.windowMinutes / 30)
                delegate: Text {
                    width: 30 * grid.pxPerMinute
                    text: guideRoot.formatTime(guideRoot.shiftMinutes(guideRoot.windowStart, index * 30))
                    color: root.tertiaryColor
                    font.family: root.globalFont
                    font.pixelSize: root.sh * 0.0375
                    verticalAlignment: Text.AlignVCenter
                    height: parent.height
                }
            }
        }

        // "Now" marker: a vertical rule down the programme lanes at the current
        // time, with a tab at the top. Only visible while now falls inside the
        // scrolled window — step forward in time and it correctly disappears.
        Item {
            id: nowMarker
            visible: guideRoot.now >= guideRoot.windowStart && guideRoot.now < guideRoot.windowEnd()
            x: grid.labelWidth + ((guideRoot.now - guideRoot.windowStart) / 60000) * grid.pxPerMinute
            y: 0
            width: Math.max(2, root.sw * 0.0015)
            // Header row plus the rows actually drawn, so the rule stops with the
            // grid rather than trailing into the gap above the detail pane.
            height: grid.rowHeight + channelColumn.height
            z: 10

            Rectangle {
                anchors.fill: parent
                color: root.accentColor
            }
            // Tab at the top of the rule, sitting in the time-header row.
            Rectangle {
                width: parent.width * 4
                height: grid.rowHeight * 0.28
                x: -parent.width * 1.5
                y: 0
                color: root.accentColor
            }
        }

        ListView {
            id: channelColumn
            anchors.top: timeHeader.bottom
            width: grid.width
            // Snap to a whole number of rows so the last channel is never sliced
            // in half at the bottom edge.
            height: Math.floor((grid.height - grid.rowHeight) / grid.rowHeight) * grid.rowHeight
            model: guideRoot.channels
            clip: true
            interactive: false
            currentIndex: guideRoot.currentChannel

            delegate: Item {
                id: rowItem
                width: channelColumn.width
                height: grid.rowHeight

                property bool isCurrentChannel: index === guideRoot.currentChannel
                property string guideNumber: modelData.guideNumber

                // Channel label
                Rectangle {
                    width: grid.labelWidth
                    height: parent.height
                    color: rowItem.isCurrentChannel ? root.accentColor : "transparent"
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        leftPadding: root.sw * 0.009375
                        text: modelData.guideNumber + " " + (modelData.guideName || "")
                        color: rowItem.isCurrentChannel ? root.surfaceColor : root.primaryColor
                        font.family: root.globalFont
                        font.capitalization: Font.AllUppercase
                        font.pixelSize: root.sh * 0.0416667
                        elide: Text.ElideRight
                        width: grid.labelWidth - root.sw * 0.0125
                    }
                }

                // Programme blocks for this channel, clipped to the time window.
                Item {
                    x: grid.labelWidth
                    width: grid.laneWidth
                    height: parent.height
                    clip: true

                    // A row with nothing in the current window used to render as
                    // empty space, which is indistinguishable from a broken grid
                    // — it cost a real debugging session before the cause turned
                    // out to be guide data that had simply expired. Say so.
                    Text {
                        anchors.fill: parent
                        anchors.leftMargin: root.sw * 0.004
                        verticalAlignment: Text.AlignVCenter
                        visible: !guideRoot.hasEventsInWindow(rowItem.guideNumber)
                        text: "NO GUIDE DATA"
                        color: root.tertiaryColor
                        font.family: root.globalFont
                        font.pixelSize: root.sh * 0.0375
                    }

                    Repeater {
                        model: guideRoot.eventsFor(rowItem.guideNumber)

                        delegate: Rectangle {
                            // Offset/width in minutes from the window start; a
                            // programme starting before the window is clamped to 0
                            // so long shows still render their tail.
                            property real startMin: (new Date(modelData.start) - guideRoot.windowStart) / 60000
                            property real endMin: (new Date(modelData.end) - guideRoot.windowStart) / 60000
                            property real clampedStart: Math.max(startMin, 0)
                            property real clampedEnd: Math.min(endMin, guideRoot.windowMinutes)
                            property bool isSelected: rowItem.isCurrentChannel && index === guideRoot.currentEvent

                            visible: clampedEnd > clampedStart
                            x: clampedStart * grid.pxPerMinute
                            width: Math.max((clampedEnd - clampedStart) * grid.pxPerMinute - 2, 0)
                            height: parent.height - 2
                            y: 1
                            color: isSelected ? root.accentColor : "transparent"
                            border.color: isSelected ? root.accentColor : root.tertiaryColor
                            border.width: 1

                            Text {
                                anchors.fill: parent
                                anchors.leftMargin: root.sw * 0.004
                                anchors.rightMargin: root.sw * 0.004
                                verticalAlignment: Text.AlignVCenter
                                text: modelData.title || ""
                                color: parent.isSelected ? root.surfaceColor : root.primaryColor
                                font.family: root.globalFont
                                font.capitalization: Font.AllUppercase
                                font.pixelSize: root.sh * 0.0375
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }
        }
    }

    // Detail pane for the selected programme.
    Item {
        id: detail
        visible: grid.visible && guideRoot.selectedEvent() !== null
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: footer.top
        anchors.leftMargin: root.sw * 0.125
        anchors.rightMargin: root.sw * 0.125
        anchors.bottomMargin: root.sh * 0.015
        height: root.sh * 0.175

        Text {
            id: detailTitle
            width: parent.width
            text: {
                var ev = guideRoot.selectedEvent()
                if (!ev) return ""
                return guideRoot.formatTime(new Date(ev.start)) + "-" +
                       guideRoot.formatTime(new Date(ev.end)) + "  " + (ev.title || "")
            }
            color: root.primaryColor
            font.family: root.globalFont
            font.capitalization: Font.AllUppercase
            font.pixelSize: root.sh * 0.0416667
            elide: Text.ElideRight
        }

        Text {
            anchors.top: detailTitle.bottom
            anchors.topMargin: root.sh * 0.008
            width: parent.width
            text: {
                var ev = guideRoot.selectedEvent()
                return ev && ev.description ? ev.description : ""
            }
            color: root.tertiaryColor
            font.family: root.globalFont
            font.pixelSize: root.sh * 0.0333333
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
        }
    }

    Text {
        id: footer
        text: root.hints.back + ":BACK " + root.hints.navigate + ":NAVIGATE " +
              root.hints.select + ":WATCH  C:CHANNELS"
        color: root.tertiaryColor
        font.family: root.globalFont
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.bottomMargin: root.sh * 0.07
        anchors.leftMargin: root.sw * 0.125
        font.pixelSize: root.sh * 0.0333333
    }
}

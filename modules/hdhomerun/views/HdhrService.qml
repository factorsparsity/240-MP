import QtQuick

// HdhrService — the module's service layer for the views.
//
// The tuner is a plain HTTP server, so lineup, tuning and stream URLs are done
// here in QML. What QML cannot do (read the guide cache from disk, find tuners
// by UDP broadcast) is delegated to hdhomerunBackend
// (src/modules/hdhomerun/HDHomeRunBackend).
//
// load_channels/tune_channel/load_guide in, channelsLoaded/guideLoaded/
// streamUrlReady/errorOccurred out.
//
// Reached by views as an injected property, not a singleton — Root.qml owns the
// single instance and passes it through the Loader. Upstream declares no QML
// singletons anywhere, so this avoids relying on an unproven qmldir path.
QtObject {
    id: svc

    // Must match manifest.json's "id" — it is the key appCore stores settings under.
    readonly property string moduleId: "com.240mp.hdhomerun"

    signal channelsLoaded(var channels)
    signal guideLoaded(var guide)
    signal streamUrlReady(string url)
    signal errorOccurred(string message)
    // For the in-module tuner picker; the Settings row gets the same list from
    // the backend's get_devices options_slot.
    signal devicesDiscovered(var devices)

    // Cached from the last load_channels(), exactly as the C++ m_channels was —
    // tune_channel() resolves against it rather than re-fetching.
    property var _channels: []

    // True when the configured device has the hardware H.264 encoder. Never send
    // ?transcode= to a device that lacks it: a non-transcoding DUAL (HDHR-US) sits
    // on this LAN and would be handed a parameter it never advertised.
    property bool _canTranscode: false

    // Every HDHomeRun on the LAN, for falling back when the preferred one has no
    // free tuner. _devices is the last discovery ([{ip, canTranscode}]),
    // _lineups maps ip -> {guideNumber: true}, so a device is only tried for
    // channels it carries. Both fill in the background after load_channels();
    // until then the configured device is the only candidate.
    property var _devices: []
    property var _lineups: ({})
    // The device the last stream came from, for reportPlayFailure().
    property string _tunedIp: ""

    // Discovery gets a deadline of its own, so the picker can never be left on
    // "SEARCHING..." if the backend's reply is somehow lost.
    property bool _discovering: false
    property Timer _discoveryDeadline: Timer {
        interval: 12000
        onTriggered: {
            if (!svc._discovering)
                return
            svc._discovering = false
            svc.errorOccurred("DISCOVERY TIMED OUT")
            svc.devicesDiscovered([])
        }
    }

    // Single exit point so the deadline and the in-flight flag can never be left
    // set by whichever path finishes first.
    function _finishDiscovery(devices) {
        if (!_discovering)
            return
        _discovering = false
        _discoveryDeadline.stop()
        console.log("[HDHomeRun] discovered " + devices.length + " tuner(s): "
                    + devices.map(function(d) { return d.ip }).join(", "))
        devicesDiscovered(devices)
    }

    // ---------------------------------------------------------------------
    // Settings — via appCore, which is a global context property and so is
    // reachable without any backend of our own. This replaces the C++
    // loadConfig()/saveConfig() direct file I/O entirely.
    // ---------------------------------------------------------------------

    function _setting(key, fallback) {
        var v = appCore.get_setting(moduleId, key)
        return (v === undefined || v === null || v === "") ? fallback : v
    }

    function deviceIp() { return _setting("device_id", "") }

    // Default duplicated in manifest.json by necessity: the key is absent from
    // config.json until the user changes it, so there must be a fallback here too.
    function transcodeProfile() { return _setting("transcode_profile", "internet480") }
    function backupIp() { return _setting("backup_device_id", "auto") }

    // Persist a chosen tuner and immediately reload against it.
    function selectDevice(ip) {
        appCore.save_setting(moduleId, "device_id", ip)
        load_channels()
    }

    // ---------------------------------------------------------------------
    // HTTP
    // ---------------------------------------------------------------------

    function _getJson(url, onOk, onErr) {
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE)
                return
            var ok = (xhr.status === 200)
            if (!ok) {
                onErr("HTTP " + xhr.status)
                return
            }
            var parsed
            try {
                parsed = JSON.parse(xhr.responseText)
            } catch (e) {
                onErr("BAD JSON")
                return
            }
            onOk(parsed)
        }
        xhr.open("GET", url)
        xhr.send()
    }

    // ---------------------------------------------------------------------
    // Browse
    // ---------------------------------------------------------------------

    function load_channels() {
        var ip = deviceIp()
        if (ip === "") {
            errorOccurred("NO HDHOMERUN DEVICE CONFIGURED")
            return
        }
        // Probe the model before the lineup, chained rather than raced: tune_channel()
        // must know whether ?transcode= is legal before the user can pick a channel.
        _getJson("http://" + ip + "/discover.json",
                 function(d) { _canTranscode = _supportsTranscode(d); _fetchLineup(ip) },
                 function()  { _canTranscode = false;                 _fetchLineup(ip) })
        // Straight to the backend, not discoverDevices(): that one drives the
        // picker and its timeout raises errorOccurred, which would end playback.
        hdhomerunBackend.discover_devices()
    }

    // discover.json exposes no capability field, so the model/firmware string is
    // the only local signal. Same rule as the backend's supportsTranscode().
    function _supportsTranscode(d) {
        var model = String(d.ModelNumber || "").toUpperCase()
        var fw    = String(d.FirmwareName || "").toLowerCase()
        return model.indexOf("HDTC") === 0 || fw.indexOf("hdhomeruntc") >= 0
    }

    function _fetchLineup(ip) {
        _getJson("http://" + ip + "/lineup.json", function(arr) {
            var out = []
            for (var i = 0; i < arr.length; i++) {
                out.push({
                    guideNumber: arr[i].GuideNumber || "",
                    guideName:   arr[i].GuideName   || "",
                    streamUrl:   arr[i].URL         || ""
                })
            }
            _channels = out
            // The only success-path log in the module. Worth it: on a headless
            // Pi this is the one line that distinguishes "tuner unreachable"
            // from "tuner fine, UI broken".
            console.log("[HDHomeRun] " + out.length + " channels from " + ip
                        + (_canTranscode ? " (transcode " + transcodeProfile() + ")" : " (no transcoder)"))
            channelsLoaded(out)
        }, function(msg) {
            errorOccurred("LINEUP FETCH FAILED: " + msg)
        })
    }

    function tune_channel(guideNumber) {
        for (var i = 0; i < _channels.length; i++) {
            if (_channels[i].guideNumber !== guideNumber)
                continue
            var lineupUrl = String(_channels[i].streamUrl).split("?")[0]
            _tuneFirstFree(_candidates(guideNumber, lineupUrl), 0, false)
            return
        }
        errorOccurred("CHANNEL NOT FOUND: " + guideNumber)
    }

    // Tuner, then Backup Tuner, then every other device in discovery order;
    // devices that can't transcode always come after those that can, whatever
    // the settings say, because they send full-rate MPEG-2 that the Pi
    // decodes in software.
    function _candidates(guideNumber, lineupUrl) {
        var primary = deviceIp()
        var backup = backupIp()
        var list = [{ ip: primary, canTranscode: _canTranscode, url: lineupUrl }]
        var others = _devices.filter(function(d) {
            return d.ip !== primary && _lineups[d.ip] && _lineups[d.ip][guideNumber]
        })
        others.sort(function(a, b) { return (b.ip === backup ? 1 : 0) - (a.ip === backup ? 1 : 0) })
        others.forEach(function(d) {
            list.push({ ip: d.ip, canTranscode: d.canTranscode,
                        url: "http://" + d.ip + ":5004/auto/v" + guideNumber })
        })
        return list.filter(function(c) { return c.canTranscode })
                   .concat(list.filter(function(c) { return !c.canTranscode }))
    }

    // A device answers 503 when all its tuners are taken; status.json shows a
    // taken tuner as one with a TargetIP. When no device has one free, the
    // guide says so instead of trying a stream that would be refused.
    function _tuneFirstFree(candidates, n, anyReachable) {
        if (n >= candidates.length) {
            playFailure = ""
            playFailure = anyReachable ? "TUNERS BUSY" : "NO TUNER"
            errorOccurred(anyReachable ? "ALL TUNERS BUSY" : "NO TUNER REACHABLE")
            return
        }
        var c = candidates[n]
        _getJson("http://" + c.ip + "/status.json", function(tuners) {
            if (!tuners.some(function(t) { return t.TargetIP === undefined })) {
                _tuneFirstFree(candidates, n + 1, true)
                return
            }
            if (n > 0)
                console.log("[HDHomeRun] using " + c.ip + "; earlier choices have no free tuner")
            _streamFrom(c)
        }, function(msg) {
            // Older firmware (the DUAL here) has no status.json: the device is
            // up but can't say whether a tuner is free, so try it.
            if (msg === "HTTP 404") _streamFrom(c)
            else _tuneFirstFree(candidates, n + 1, anyReachable)
        })
    }

    function _streamFrom(c) {
        // Omitting ?transcode= does NOT give the raw broadcast — the device
        // applies its own configured default (measured: this EXTEND returns
        // 1080p H.264 for a bare lineup URL). So "none" is a real choice
        // meaning MPEG-2, not "no preference", and leaving it off is not neutral.
        var url = c.url
        if (c.canTranscode)
            url += "?transcode=" + transcodeProfile()
        _tunedIp = c.ip

        // One argument, not Plex's (url, token): AppCore never wires this
        // signal, so its shape is this module's own choice, and HDHomeRun LAN
        // streams are unauthenticated.
        streamUrlReady(url)
    }

    function _loadLineups(devices) {
        _devices = devices
        devices.forEach(function(d) {
            _getJson("http://" + d.ip + "/lineup.json", function(arr) {
                var numbers = {}
                arr.forEach(function(ch) { if (ch.GuideNumber) numbers[ch.GuideNumber] = true })
                var all = _lineups
                all[d.ip] = numbers
                _lineups = all
            }, function() {})
        })
    }

    // ---------------------------------------------------------------------
    // Front panel
    // ---------------------------------------------------------------------

    // Push a channel number to the 7-segment display daemon (modules/segdisplay).
    // Lives here rather than in each view because the service is the one object
    // every view already holds, and because the number is this module's alone —
    // every other module shows an elapsed timer instead.
    //
    // Silent by design: the panel is optional hardware that may not be wired up,
    // and this fires on every cursor move. SegHook.qml does the "is it there"
    // logging once, so a missing daemon is still diagnosable.
    // The URL is duplicated in segdisplay's own QML; see CLAUDE.md.
    property var _panelPending: []

    function announceChannel(guideNumber) {
        if (!guideNumber)
            return
        var xhr = new XMLHttpRequest()
        // Held until done: a collected XMLHttpRequest is reset before it sends.
        _panelPending.push(xhr)
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE)
                return
            var i = svc._panelPending.indexOf(xhr)
            if (i >= 0)
                svc._panelPending.splice(i, 1)
        }
        xhr.open("POST", "http://127.0.0.1:8377/state")
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(JSON.stringify({ channel: String(guideNumber) }))
    }

    // ---------------------------------------------------------------------
    // EPG
    // ---------------------------------------------------------------------

    // The cache is { "_multiplexes": {...}, "channels": { "2.1": [events] } };
    // the backend returns just "channels" ({} before the first scan).
    function load_guide() {
        guideLoaded(hdhomerunBackend.read_guide())
    }

    // Why the last channel didn't play, for the guide to show. mpv only reports
    // "failed"; the usual cause is HTTP 503 because every tuner is in use (by
    // other clients or a guide scan), which status.json shows as a TargetIP on
    // each tuner.
    property string playFailure: ""

    function reportPlayFailure() {
        playFailure = ""
        _getJson("http://" + (_tunedIp || deviceIp()) + "/status.json",
                 function(tuners) {
                     var busy = tuners.length > 0
                             && tuners.every(function(t) { return t.TargetIP !== undefined })
                     playFailure = busy ? "TUNERS BUSY" : "PLAY FAILED"
                 },
                 function(msg) { playFailure = msg === "HTTP 404" ? "PLAY FAILED" : "NO TUNER" })
    }

    // "started" | "running" | "failed"; see HDHomeRunBackend::start_guide_scan.
    function startGuideScan() { return hdhomerunBackend.start_guide_scan() }
    function scanStatus()     { return hdhomerunBackend.scan_status() }

    // ---------------------------------------------------------------------
    // Device discovery
    // ---------------------------------------------------------------------

    // UDP broadcast on the LAN, done by the backend: works without internet.
    function discoverDevices() {
        _discovering = true
        _discoveryDeadline.restart()
        hdhomerunBackend.discover_devices()
    }

    property Connections _backend: Connections {
        target: hdhomerunBackend
        function onDevicesDiscovered(devices) {
            svc._loadLineups(devices)
            svc._finishDiscovery(devices)
        }
    }
}

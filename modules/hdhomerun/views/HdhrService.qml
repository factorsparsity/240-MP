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

            // Omitting ?transcode= does NOT give the raw broadcast — the device
            // applies its own configured default (measured: this EXTEND returns
            // 1080p H.264 for a bare lineup URL). So "none" is a real choice
            // meaning MPEG-2, not "no preference", and leaving it off is not neutral.
            var url = String(_channels[i].streamUrl).split("?")[0]
            if (_canTranscode)
                url += "?transcode=" + transcodeProfile()

            // One argument, not Plex's (url, token): AppCore never wires this
            // signal, so its shape is this module's own choice, and HDHomeRun LAN
            // streams are unauthenticated.
            streamUrlReady(url)
            return
        }
        errorOccurred("CHANNEL NOT FOUND: " + guideNumber)
    }

    // ---------------------------------------------------------------------
    // EPG
    // ---------------------------------------------------------------------

    // The cache is { "_multiplexes": {...}, "channels": { "2.1": [events] } };
    // the backend returns just "channels" ({} before the first scan).
    function load_guide() {
        guideLoaded(hdhomerunBackend.read_guide())
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
        function onDevicesDiscovered(devices) { svc._finishDiscovery(devices) }
    }
}

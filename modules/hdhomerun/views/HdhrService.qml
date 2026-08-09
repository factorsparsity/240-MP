import QtQuick

// HdhrService — the whole HDHomeRun backend, in QML/JS.
//
// This replaces src/HDHomeRunBackend.{h,cpp}. The point is that 240-MP discovers
// modules purely from the filesystem (AppCore.cpp:15-32 scans appRoot/modules for
// any dir with a manifest.json) and registerModule explicitly tolerates a null
// backend, so a module that needs no C++ can be dropped into ANY stock 240-MP
// install — including upstream's prebuilt release tarball — with no recompile.
// A compiled backend would pin us to a self-built binary forever.
//
// It keeps the old backend's signal/method names so the views changed as little
// as possible: load_channels/tune_channel/load_guide in, channelsLoaded/
// guideLoaded/streamUrlReady/errorOccurred out.
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
    // No C++ equivalent: device discovery used to populate a settings dropdown via
    // dynamicOptionsReady. That path needs a backend, so discovery moved into the
    // module's own UI and reports here instead.
    signal devicesDiscovered(var devices)

    // Cached from the last load_channels(), exactly as the C++ m_channels was —
    // tune_channel() resolves against it rather than re-fetching.
    property var _channels: []

    // True when the configured device has the hardware H.264 encoder. Never send
    // ?transcode= to a device that lacks it: a non-transcoding DUAL (HDHR-US) sits
    // on this LAN and would be handed a parameter it never advertised.
    property bool _canTranscode: false

    // Discovery needs a deadline of its own. QML's XMLHttpRequest has no working
    // timeout, so a hung request leaves the picker on "SEARCHING..." forever —
    // and the case most likely to hang is a box with no internet, which is
    // precisely when the user needs to be told rather than left waiting.
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

    // status 0 with a body is a successful file:// read — Qt reports no HTTP
    // status for local files, and load_guide() depends on that case.
    function _getJson(url, onOk, onErr) {
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE)
                return
            var ok = (xhr.status === 200) || (xhr.status === 0 && xhr.responseText !== "")
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
    // the only local signal. (The cloud discovery API does better — see
    // discoverDevices(), which carries an explicit Transcode flag.)
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

    // Mirrors main.cpp's resolveDataRoot(): QStandardPaths::AppDataLocation with
    // applicationName "240-MP" and no organization. Known gap vs. the C++ version,
    // which was handed dataRoot directly: a DATA_ROOT env override is invisible
    // from QML, so a non-default data dir needs epg_cache.json in the standard spot.
    function _dataRoot() {
        var home = appCore.homePath()
        return Qt.platform.os === "osx"
            ? home + "/Library/Application Support/240-MP"
            : home + "/.local/share/240-MP"
    }

    // The cache is { "_multiplexes": {...}, "channels": { "2.1": [events] } }.
    // Only "channels" is guide data; "_multiplexes" is the scanner's cached RF map,
    // which exists because lineup.json exposes no RF number.
    function load_guide() {
        var url = "file://" + _dataRoot() + "/epg_cache.json"
        _getJson(url,
                 function(d) {
                     var ch = d.channels || ({})
                     console.log("[HDHomeRun] guide loaded: " + Object.keys(ch).length + " channels from " + url)
                     guideLoaded(ch)
                 },
                 function(msg) {
                     // Silent failure here reads as "no guide data" in the UI,
                     // which is indistinguishable from "never scanned" — so say
                     // which file we tried and why it failed.
                     console.log("[HDHomeRun] guide read FAILED (" + msg + ") for " + url)
                     guideLoaded({})
                 })
    }

    // ---------------------------------------------------------------------
    // Device discovery
    // ---------------------------------------------------------------------

    // The C++ version broadcast a CRC-32'd UDP packet on port 65001. QML has no
    // UDP, so this uses HDHomeRun's HTTP discovery service, which returns every
    // device sharing the caller's public IP. Verified to return the same three
    // devices the UDP sweep found on this LAN — and it is strictly better in one
    // way: each entry carries an explicit "Transcode" flag, where the local
    // discover.json forces us to string-match the model number.
    //
    // Tradeoff, and it is a real one: this needs working internet, where UDP
    // broadcast was LAN-only. selectDevice() accepts a hand-typed IP for that case.
    function discoverDevices() {
        _discovering = true
        _discoveryDeadline.restart()

        _getJson("https://ipv4-api.hdhomerun.com/discover", function(list) {
            if (!list || list.length === 0) {
                _finishDiscovery([])
                return
            }
            var devices = []
            var remaining = list.length

            for (var i = 0; i < list.length; i++)
                _describe(list[i], devices, function() {
                    if (--remaining === 0) {
                        devices.sort(function(a, b) { return a.ip < b.ip ? -1 : 1 })
                        _finishDiscovery(devices)
                    }
                })
        }, function(msg) {
            if (!_discovering)
                return
            errorOccurred("DISCOVERY FAILED: " + msg)
            _finishDiscovery([])
        })
    }

    // Resolve one device's friendly name and tuner count. Failure is not fatal —
    // a device that answered discovery but not HTTP is still selectable by IP.
    function _describe(entry, out, done) {
        var ip = entry.LocalIP || ""
        var canTranscode = (entry.Transcode === 1)
        if (ip === "") { done(); return }

        _getJson("http://" + ip + "/discover.json", function(d) {
            var label = (d.FriendlyName || "HDHOMERUN") + " " + ip
            if (d.TunerCount > 0)
                label += " (" + d.TunerCount + " TUNERS)"
            out.push({ ip: ip, label: label.toUpperCase(),
                       canTranscode: canTranscode || _supportsTranscode(d) })
            done()
        }, function() {
            out.push({ ip: ip, label: ip, canTranscode: canTranscode })
            done()
        })
    }
}

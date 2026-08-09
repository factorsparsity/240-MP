import QtQuick

// Module shell — same nav-stack pattern every 240-MP module uses (see
// modules/ambient_mode/views/Root.qml). Nothing HDHomeRun-specific lives here;
// it just owns the Loader and the back-stack, and opens Channels.qml first.
FocusScope {
    id: moduleRoot

    signal goBack()

    property var navParams: ({})

    // The module's manifest id — the single place it appears in this module's QML.
    // Child views reference it via moduleRoot.moduleId.
    property string moduleId: "com.240mp.hdhomerun"
    property var _moduleInfo: appCore ? appCore.get_module_info(moduleId) : ({})
    property string moduleName: _moduleInfo.name || ""
    property string moduleIcon: _moduleInfo.icon || ""

    property var navStack: []
    property var currentParams: ({})

    // The module's entire backend. Was a C++ context property (hdhomerunBackend)
    // registered from main.cpp; now a plain QML object owned here, which is what
    // lets this module drop into a stock 240-MP with no recompile. One instance,
    // injected into every view through the Loader below so navigation does not
    // reset the cached channel list.
    property HdhrService hdhr: HdhrService {}

    function navigateTo(viewPath, params, fromState) {
        var resolved = Qt.resolvedUrl(viewPath)
        navStack.push({ source: internalLoader.source, params: currentParams, listState: fromState || {} })
        currentParams = params || {}
        internalLoader.setSource(resolved, { "navParams": params || {}, "hdhr": hdhr })
    }

    function navigateBack() {
        if (navStack.length === 0) {
            moduleRoot.goBack()
            return
        }
        var prev = navStack.pop()
        if (!prev.source || prev.source.toString() === "") {
            moduleRoot.goBack()
            return
        }
        var restored = Object.assign({}, prev.params)
        restored.navListState = prev.listState || {}
        currentParams = restored
        internalLoader.setSource(prev.source, { "navParams": restored, "hdhr": hdhr })
    }

    Loader {
        id: internalLoader
        anchors.fill: parent
        focus: true
        onLoaded: { if (item) item.forceActiveFocus() }

        Connections {
            target: internalLoader.item
            ignoreUnknownSignals: true
            function onNavigateTo(path, params, listState) { moduleRoot.navigateTo(path, params, listState) }
            function onGoBack() { moduleRoot.navigateBack() }
        }
    }

    // Which view opens first, per the start_view setting (default Guide — the
    // guide is what a cable box shows you). Back from the first view exits the
    // module either way; > and < still move between the two.
    Component.onCompleted: {
        var start = appCore.get_setting(moduleId, "start_view") || "Guide"
        navigateTo(start === "Channels" ? "Channels.qml" : "Guide.qml", {})
    }
}

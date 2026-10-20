#pragma once

#include <QNetworkAccessManager>
#include <QObject>
#include <QString>
#include <QVariant>
#include <QVariantList>
#include <QVariantMap>

#include <functional>

// Backend for the HDHomeRun module.
//
// Deliberately small: the tuner is a plain HTTP server, so lineup, tuning and
// stream URLs stay in views/HdhrService.qml. This class covers only what QML
// cannot do:
//   - read the guide cache from disk (no QML_XHR_ALLOW_FILE_READ needed),
//   - start a guide scan (scripts/epg-rescan-trigger.py) from the settings action,
//   - find tuners by UDP broadcast, which works without internet, for both the
//     Tuner setting and the in-module picker.
class HDHomeRunBackend : public QObject {
    Q_OBJECT
public:
    explicit HDHomeRunBackend(const QString &appRoot, const QString &dataRoot,
                              QObject *parent = nullptr);

    Q_INVOKABLE QString data_root() const { return m_dataRoot; }

    // The "channels" map from epg_cache.json ({ "2.1": [events], ... }), or an
    // empty map when there is no cache yet.
    Q_INVOKABLE QVariantMap read_guide() const;

    // "Rescan Guide Data" settings action; same as start_guide_scan().
    Q_INVOKABLE void rescan_guide();

    // Starts a scan, detached so a ~90 minute scan outlives the app. Returns
    // "started", "running" (one already holds the scan lock) or "failed".
    Q_INVOKABLE QString start_guide_scan();

    // { running: bool, detail: "3/12" | "FINDING CHANNELS" | "",
    //   updatedMs: epg_cache.json mtime (0 if none) }. Running is read from the
    // scan lock, so it also covers scans started by the nightly timer; detail
    // comes from epg_scan.log, which only in-app scans write.
    Q_INVOKABLE QVariantMap scan_status() const;

    // "Tuner" options_slot: emits dynamicOptionsReady("device_id", [{id, label}])
    // and the "transcode" capability for the selected tuner.
    Q_INVOKABLE void get_devices();

    // "Tuner" apply_slot: re-evaluates the "transcode" capability for the newly
    // selected tuner from the last discovery, so Video Quality appears or
    // disappears as the user changes Tuner.
    Q_INVOKABLE void apply_device();

    // In-module picker: emits devicesDiscovered([{ip, label, canTranscode}]).
    Q_INVOKABLE void discover_devices();

signals:
    void dynamicOptionsReady(const QString &key, const QVariant &options);
    void devicesDiscovered(const QVariant &devices);

private:
    // Broadcast, collect replies for a moment, then describe each tuner from its
    // discover.json. Calls done exactly once, sorted by IP.
    void discover(std::function<void(const QVariantList &)> done);
    QString selectedDevice() const;
    bool scanRunning() const;
    void emitTranscodeCapability();

    QString m_appRoot;
    QString m_dataRoot;
    QNetworkAccessManager m_nam;
    QVariantList m_devices;   // last discovery result: [{ip, label, canTranscode}]

    static constexpr int kDiscoveryWindowMs = 1500;
    static constexpr int kDescribeTimeoutMs = 3000;
};

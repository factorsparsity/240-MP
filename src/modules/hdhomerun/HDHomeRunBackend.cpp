#include "HDHomeRunBackend.h"

#include <QDateTime>
#include <QFile>
#include <QFileInfo>
#include <QHostAddress>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProcess>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QTimer>
#include <QUdpSocket>

#include <algorithm>
#include <memory>

#include <cerrno>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

static const char *kModuleId = "com.240mp.hdhomerun";

HDHomeRunBackend::HDHomeRunBackend(const QString &appRoot, const QString &dataRoot, QObject *parent)
    : QObject(parent), m_appRoot(appRoot), m_dataRoot(dataRoot)
{
}

QString HDHomeRunBackend::selectedDevice() const {
    QFile f(m_dataRoot + "/config.json");
    if (!f.open(QIODevice::ReadOnly))
        return {};
    const QJsonObject cfg = QJsonDocument::fromJson(f.readAll()).object();
    return cfg.value("modules").toObject().value(kModuleId).toObject()
              .value("device_id").toString();
}

// ---------------------------------------------------------------------------
// Guide
// ---------------------------------------------------------------------------

// The cache is { "_multiplexes": {...}, "channels": { "2.1": [events] } }.
// Only "channels" is guide data; "_multiplexes" is the scanner's cached RF map.
QVariantMap HDHomeRunBackend::read_guide() const {
    const QString path = m_dataRoot + "/epg_cache.json";
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) {
        qDebug("[HDHomeRun] no guide cache at %s", qPrintable(path));
        return {};
    }
    const QVariantMap channels = QJsonDocument::fromJson(f.readAll()).object()
                                     .value("channels").toObject().toVariantMap();
    qDebug("[HDHomeRun] guide loaded: %lld channels", qlonglong(channels.size()));
    return channels;
}

// Must match LOCK_PATH in scripts/epg-rescan-trigger.py.
static QString scanLockPath() {
    const QString env = qEnvironmentVariable("EPG_SCAN_LOCK");
    return env.isEmpty() ? QStringLiteral("/tmp/240mp-epg-scan.lock") : env;
}

// The trigger holds a flock on the lock file for the whole scan, so failing to
// take it here means a scan is running. Taken and released immediately.
bool HDHomeRunBackend::scanRunning() const {
    const int fd = ::open(QFile::encodeName(scanLockPath()).constData(), O_RDONLY);
    if (fd < 0)
        return false;   // no lock file: no scan has run since boot
    const bool held = ::flock(fd, LOCK_EX | LOCK_NB) != 0 && errno == EWOULDBLOCK;
    ::close(fd);        // closing releases our lock if we took it
    return held;
}

QVariantMap HDHomeRunBackend::scan_status() const {
    QVariantMap st;
    const bool running = scanRunning();
    st["running"] = running;
    const QFileInfo cache(m_dataRoot + "/epg_cache.json");
    st["updatedMs"] = cache.exists() ? cache.lastModified().toMSecsSinceEpoch() : qint64(0);

    QString phase;
    int step = 0, total = 0;
    QFile log(m_dataRoot + "/epg_scan.log");
    if (running && log.open(QIODevice::ReadOnly)) {
        // Only the current run matters, and it is at the end of the file. The
        // scanner prints "Probing RF n [i/N]" during the one-time sweep, then
        // "Scanning RF n [i/N] (...)" per multiplex; the last such line wins.
        log.seek(qMax<qint64>(0, log.size() - 16384));
        const QString tail = QString::fromUtf8(log.readAll());
        const QString run = tail.mid(qMax(0, tail.lastIndexOf("scan starting")));
        static const QRegularExpression progress(
            QStringLiteral(R"((Probing|Scanning) RF \d+ \[(\d+)/(\d+)\])"));
        QRegularExpressionMatchIterator it = progress.globalMatch(run);
        QRegularExpressionMatch last;
        while (it.hasNext())
            last = it.next();
        if (last.hasMatch()) {
            phase = last.captured(1) == QLatin1String("Probing") ? "sweep" : "capture";
            step  = last.captured(2).toInt();
            total = last.captured(3).toInt();
        }
    }
    st["phase"] = phase;
    st["step"]  = step;
    st["total"] = total;
    return st;
}

void HDHomeRunBackend::rescan_guide() {
    start_guide_scan();
}

QString HDHomeRunBackend::start_guide_scan() {
    if (scanRunning())
        return QStringLiteral("running");
    const QString python = QStandardPaths::findExecutable("python3");
    if (python.isEmpty()) {
        qWarning("[HDHomeRun] rescan: python3 not found");
        return QStringLiteral("failed");
    }
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert("MP240_CONFIG", m_dataRoot + "/config.json");
    env.insert("EPG_CACHE_PATH", m_dataRoot + "/epg_cache.json");
    env.insert("PYTHONUNBUFFERED", "1");

    const QString log = m_dataRoot + "/epg_scan.log";
    QProcess proc;
    proc.setProgram(python);
    proc.setArguments({m_appRoot + "/modules/hdhomerun/scripts/epg-rescan-trigger.py"});
    proc.setProcessEnvironment(env);
    proc.setStandardOutputFile(log, QIODevice::Append);
    proc.setStandardErrorFile(log, QIODevice::Append);
    if (!proc.startDetached()) {
        qWarning("[HDHomeRun] guide scan failed to start");
        return QStringLiteral("failed");
    }
    qDebug("[HDHomeRun] guide scan started, logging to %s", qPrintable(log));
    return QStringLiteral("started");
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

// discover.json exposes no capability field, so the model/firmware string is
// the only signal: EXTEND models (HDTC-*) carry the hardware transcoder.
static bool supportsTranscode(const QJsonObject &discover) {
    return discover.value("ModelNumber").toString().startsWith("HDTC", Qt::CaseInsensitive)
        || discover.value("FirmwareName").toString().contains("hdhomeruntc", Qt::CaseInsensitive);
}

// CRC-32 (reflected, poly 0xEDB88320) — the device ignores a discovery packet
// whose trailing CRC is wrong, so this has to be right or nothing replies.
static quint32 hdhrCrc32(const QByteArray &data) {
    static quint32 table[256];
    static bool built = false;
    if (!built) {
        for (quint32 i = 0; i < 256; ++i) {
            quint32 c = i;
            for (int k = 0; k < 8; ++k)
                c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            table[i] = c;
        }
        built = true;
    }
    quint32 crc = 0xFFFFFFFFu;
    for (unsigned char b : data)
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

// HDHomeRun discovery request: [type:2 BE][len:2 BE][TLV payload][crc:4 LE].
// TLV 0x01 = device type (1 = tuner), TLV 0x02 = device id (0xFFFFFFFF = any).
static QByteArray discoveryPacket() {
    static const unsigned char payload[] = {
        0x01, 0x04, 0x00, 0x00, 0x00, 0x01,
        0x02, 0x04, 0xFF, 0xFF, 0xFF, 0xFF,
    };
    QByteArray pkt;
    pkt.append(char(0x00)); pkt.append(char(0x02));                 // type 0x0002
    pkt.append(char(0x00)); pkt.append(char(sizeof(payload)));      // payload length
    pkt.append(reinterpret_cast<const char *>(payload), sizeof(payload));
    const quint32 crc = hdhrCrc32(pkt);
    pkt.append(char(crc & 0xFF));
    pkt.append(char((crc >> 8) & 0xFF));
    pkt.append(char((crc >> 16) & 0xFF));
    pkt.append(char((crc >> 24) & 0xFF));
    return pkt;
}

void HDHomeRunBackend::discover(std::function<void(const QVariantList &)> done) {
    auto *sock = new QUdpSocket(this);
    if (!sock->bind(QHostAddress::AnyIPv4, 0, QUdpSocket::ShareAddress)) {
        qWarning("[HDHomeRun] discovery socket bind failed");
        sock->deleteLater();
        done({});
        return;
    }

    auto ips = std::make_shared<QStringList>();
    connect(sock, &QUdpSocket::readyRead, this, [sock, ips]() {
        while (sock->hasPendingDatagrams()) {
            QByteArray buf(int(sock->pendingDatagramSize()), Qt::Uninitialized);
            QHostAddress sender;
            sock->readDatagram(buf.data(), buf.size(), &sender);
            // The responder's address is all we need; the reply's BASE_URL tag
            // would only yield the same host.
            QString ip = sender.toString();
            if (ip.startsWith("::ffff:"))
                ip = ip.mid(7);   // IPv4-mapped
            if (!ip.isEmpty() && !ips->contains(ip))
                ips->append(ip);
        }
    });
    sock->writeDatagram(discoveryPacket(), QHostAddress::Broadcast, 65001);

    QTimer::singleShot(kDiscoveryWindowMs, this, [this, sock, ips, done]() {
        sock->deleteLater();
        if (ips->isEmpty()) {
            qDebug("[HDHomeRun] discovery: no tuners answered");
            done({});
            return;
        }
        auto devices   = std::make_shared<QVariantList>();
        auto remaining = std::make_shared<int>(ips->size());
        for (const QString &ip : *ips) {
            QNetworkRequest req(QUrl("http://" + ip + "/discover.json"));
            req.setTransferTimeout(kDescribeTimeoutMs);
            QNetworkReply *reply = m_nam.get(req);
            connect(reply, &QNetworkReply::finished, this, [reply, ip, devices, remaining, done]() {
                reply->deleteLater();
                // A tuner that answered the broadcast but not HTTP is still
                // selectable by IP.
                QString label = ip;
                bool canTranscode = false;
                if (reply->error() == QNetworkReply::NoError) {
                    const QJsonObject d = QJsonDocument::fromJson(reply->readAll()).object();
                    // "EXTEND 10.0.1.62": short enough that the IP, the only
                    // thing telling two of the same model apart, survives the
                    // Settings row's truncation.
                    QString name = d.value("FriendlyName").toString();
                    name.remove(QRegularExpression("^HDHomeRun\\s*", QRegularExpression::CaseInsensitiveOption));
                    if (!name.isEmpty())
                        label = name + " " + ip;
                    canTranscode = supportsTranscode(d);
                }
                devices->append(QVariantMap{{"ip", ip}, {"label", label.toUpper()},
                                            {"canTranscode", canTranscode}});
                if (--(*remaining) == 0) {
                    std::sort(devices->begin(), devices->end(), [](const QVariant &a, const QVariant &b) {
                        return a.toMap().value("ip").toString() < b.toMap().value("ip").toString();
                    });
                    qDebug("[HDHomeRun] discovery: %lld tuner(s)", qlonglong(devices->size()));
                    done(*devices);
                }
            });
        }
    });
}

void HDHomeRunBackend::get_devices() {
    discover([this](const QVariantList &devices) {
        m_devices = devices;
        QVariantList options;
        for (const QVariant &v : devices) {
            const QVariantMap d = v.toMap();
            // id is the IP, because that is exactly what HdhrService connects to.
            options.append(QVariantMap{{"id", d.value("ip")}, {"label", d.value("label")}});
        }
        emit dynamicOptionsReady("device_id", options);
        emitTranscodeCapability();
    });
}

void HDHomeRunBackend::apply_device() {
    emitTranscodeCapability();
}

// Shows Video Quality only for a tuner with the hardware encoder.
void HDHomeRunBackend::emitTranscodeCapability() {
    const QString selected = selectedDevice();
    bool transcodes = false;
    for (const QVariant &v : m_devices)
        if (v.toMap().value("ip").toString() == selected)
            transcodes = v.toMap().value("canTranscode").toBool();
    emit dynamicOptionsReady("_capabilities",
                             transcodes ? QVariantList{QString("transcode")} : QVariantList{});
}

void HDHomeRunBackend::discover_devices() {
    discover([this](const QVariantList &devices) { emit devicesDiscovered(devices); });
}

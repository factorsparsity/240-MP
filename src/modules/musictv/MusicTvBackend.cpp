#include "MusicTvBackend.h"
#include "../../util/YtDlpLocator.h"

#include <QDateTime>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcess>
#include <QRegularExpression>
#include <QSaveFile>
#include <QTimer>

#include <memory>

static const char *kPlaylistsFileName = "musictv_playlists.txt";
static const char *kBumpersFileName   = "musictv_bumpers.txt";
static const char *kMetadataFileName  = "musictv_metadata.json";
static const char *kCacheFileName     = "musictv_cache.json";

static const QRegularExpression::PatternOptions kReOpts =
    QRegularExpression::UseUnicodePropertiesOption;

MusicTvBackend::MusicTvBackend(const QString &appRoot, const QString &dataRoot, QObject *parent)
    : QObject(parent), m_appRoot(appRoot), m_dataRoot(dataRoot)
{
}

QString MusicTvBackend::path(const char *fileName) const {
    return m_dataRoot + "/" + QString::fromLatin1(fileName);
}

// ---------------------------------------------------------------------------
// Title parsing — artist/song from "Prince - When Doves Cry (Official Video)"
// ---------------------------------------------------------------------------

static QString stripChars(QString s, const QString &chars) {
    while (!s.isEmpty() && chars.contains(s.front()))
        s.remove(0, 1);
    while (!s.isEmpty() && chars.contains(s.back()))
        s.chop(1);
    return s;
}

// Bracketed groups that are production noise rather than part of the song name.
// "(Live)", "(Acoustic)" and the like are deliberately NOT in here — they are
// part of what the viewer is watching.
static QString cleanTitle(const QString &title) {
    static const QRegularExpression noise(
        QStringLiteral(R"([\(\[]\s*[^)\]]*\b()"
                       "official|video|videoclip|audio|hd|hq|4k|remaster(?:ed)?|lyrics?|lyric|"
                       "mv|m/v|full|uncensored|explicit|clean|dirty|hq audio|music video|visualizer"
                       R"()\b[^)\]]*[\)\]])"),
        kReOpts | QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression spaces(QStringLiteral(R"(\s{2,})"), kReOpts);
    QString out = title;
    out.replace(noise, QString());
    out.replace(spaces, QStringLiteral(" "));
    return stripChars(out, QStringLiteral(" -–—|·"));
}

// -> {artist, song}. Falls back to the uploading channel as the artist.
static QPair<QString, QString> parseArtistSong(const QString &title, const QString &channel) {
    static const QRegularExpression sep(QStringLiteral(R"(\s+[-–—]\s+)"), kReOpts);
    static const QRegularExpression topic(QStringLiteral(R"(\s*-\s*Topic$)"), kReOpts);
    static const QRegularExpression vevo(QStringLiteral("VEVO$"), kReOpts);

    const QString cleaned = cleanTitle(title);
    QString artist, song;
    const QRegularExpressionMatch m = sep.match(cleaned);
    const QString left  = m.hasMatch() ? cleaned.left(m.capturedStart()).trimmed() : QString();
    const QString right = m.hasMatch() ? cleaned.mid(m.capturedEnd()).trimmed()    : QString();
    if (!left.isEmpty() && !right.isEmpty()) {
        artist = left;
        song   = right;
    } else {
        artist = channel.trimmed();
        song   = cleaned;
    }
    song = stripChars(song.trimmed(), QStringLiteral("\"“”'"));
    // YouTube channels are routinely "Prince - Topic" or "PrinceVEVO"
    artist.remove(topic);
    artist.remove(vevo);
    return {artist.trimmed(), song};
}

// ---------------------------------------------------------------------------
// Input files
// ---------------------------------------------------------------------------

QList<MusicTvBackend::Source> MusicTvBackend::readSources(const char *fileName,
                                                          const QString &kind) const {
    static const QRegularExpression playlistId(QStringLiteral(R"([?&]list=([\w-]+))"), kReOpts);
    static const QRegularExpression videoId(
        QStringLiteral(R"((?:[?&]v=|youtu\.be/|/shorts/|/embed/)([\w-]{11}))"), kReOpts);
    static const QRegularExpression bareVideo(QStringLiteral(R"(^[\w-]{11}$)"), kReOpts);
    static const QRegularExpression barePlaylist(QStringLiteral(R"(^[\w-]{12,}$)"), kReOpts);

    QList<Source> sources;
    QFile f(path(fileName));
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text))
        return sources;
    while (!f.atEnd()) {
        QString line = QString::fromUtf8(f.readLine()).trimmed();
        if (line.isEmpty() || line.startsWith('#'))
            continue;
        const int bar = line.indexOf('|');
        if (bar >= 0)
            line = line.mid(bar + 1).trimmed();
        if (line.isEmpty())
            continue;

        Source s;
        s.kind = kind;
        QRegularExpressionMatch m;
        if ((m = playlistId.match(line)).hasMatch()) {
            s.url = QStringLiteral("https://www.youtube.com/playlist?list=") + m.captured(1);
        } else if ((m = videoId.match(line)).hasMatch()) {
            s.url = QStringLiteral("https://www.youtube.com/watch?v=") + m.captured(1);
            s.isPlaylist = false;
        } else if (bareVideo.match(line).hasMatch()) {
            // A bare id: playlist ids are long and prefixed, video ids are 11 chars.
            s.url = QStringLiteral("https://www.youtube.com/watch?v=") + line;
            s.isPlaylist = false;
        } else if (barePlaylist.match(line).hasMatch()) {
            s.url = QStringLiteral("https://www.youtube.com/playlist?list=") + line;
        } else {
            s.url = line; // let yt-dlp decide
        }
        sources << s;
    }
    return sources;
}

// ---------------------------------------------------------------------------
// Cache
// ---------------------------------------------------------------------------

bool MusicTvBackend::cacheIsStale() const {
    const QFileInfo cache(path(kCacheFileName));
    if (!cache.exists())
        return true;
    const QDateTime built = cache.lastModified();
    // Saving an input file puts the edit on air at the next open.
    for (const char *input : {kPlaylistsFileName, kBumpersFileName, kMetadataFileName}) {
        const QFileInfo fi(path(input));
        if (fi.exists() && fi.lastModified() > built)
            return true;
    }
    return built.secsTo(QDateTime::currentDateTime()) > kMaxCacheAgeSecs;
}

bool MusicTvBackend::emitFromCache() {
    QFile f(path(kCacheFileName));
    if (!f.open(QIODevice::ReadOnly))
        return false;
    const QJsonObject cache = QJsonDocument::fromJson(f.readAll()).object();
    const QVariantList music = cache.value(QLatin1String("music")).toArray().toVariantList();
    if (music.isEmpty())
        return false;
    emit poolsLoaded(music, cache.value(QLatin1String("bumpers")).toArray().toVariantList());
    return true;
}

// ---------------------------------------------------------------------------
// Refresh — one yt-dlp --flat-playlist per source, in file order
// ---------------------------------------------------------------------------

void MusicTvBackend::load() {
    if (m_refreshing)
        return; // the rebuild in flight emits for this call too
    if (!cacheIsStale() && emitFromCache())
        return;

    if (ytdlp::locate(m_dataRoot).isEmpty()) {
        if (!emitFromCache())
            emit errorOccurred(QStringLiteral("YT-DLP NOT FOUND\n"
                                              "INSTALL YT-DLP TO BUILD THE PLAYLIST"));
        return;
    }

    m_queue = readSources(kPlaylistsFileName, QStringLiteral("music"));
    if (m_queue.isEmpty()) {
        emit errorOccurred(QStringLiteral("NO PLAYLISTS FOUND\n"
                                          "ADD YOUTUBE PLAYLIST URLS TO\n"
                                          "MUSICTV_PLAYLISTS.TXT IN THE DATA DIRECTORY"));
        return;
    }
    m_queue += readSources(kBumpersFileName, QStringLiteral("bumper"));

    m_overrides = QJsonObject();
    QFile meta(path(kMetadataFileName));
    if (meta.open(QIODevice::ReadOnly)) {
        QJsonParseError err;
        const QJsonDocument doc = QJsonDocument::fromJson(meta.readAll(), &err);
        if (err.error == QJsonParseError::NoError)
            m_overrides = doc.object();
        else
            qWarning("[MusicTV] ignoring unreadable %s: %s",
                     kMetadataFileName, qPrintable(err.errorString()));
    }

    m_music.clear();
    m_bumpers.clear();
    m_seen.clear();
    m_refreshing = true;
    emit refreshStarted();
    fetchNext();
}

void MusicTvBackend::fetchNext() {
    if (m_queue.isEmpty()) {
        finishRefresh();
        return;
    }
    const Source source = m_queue.takeFirst();

    auto *proc = new QProcess(this);
    QStringList args{
        QStringLiteral("--flat-playlist"),
        QStringLiteral("--no-warnings"),
        // One JSON object per entry — robust against '|' etc. in titles
        QStringLiteral("--print"),
        QStringLiteral("%(.{id,title,channel,uploader,duration})j"),
    };
    if (source.isPlaylist)
        args << QStringLiteral("-I") << QStringLiteral("1:%1").arg(kMaxPlaylistItems);
    args << QStringLiteral("--") << source.url;

    // finished() and errorOccurred(FailedToStart) are exclusive, but a timeout
    // kill can be followed by finished(); the guard keeps it to one call.
    auto done = std::make_shared<bool>(false);
    auto finish = [this, proc, source, done]() {
        if (*done)
            return;
        *done = true;
        proc->deleteLater();

        int added = 0;
        const QList<QByteArray> lines = proc->readAllStandardOutput().split('\n');
        for (const QByteArray &line : lines) {
            const QJsonObject obj = QJsonDocument::fromJson(line.trimmed()).object();
            const QString id    = obj.value(QLatin1String("id")).toString();
            const QString title = obj.value(QLatin1String("title")).toString().trimmed();
            // Tombstones YouTube leaves in place of removed entries. A flat listing
            // reports the named forms for some and an *empty* title for others
            // (terminated accounts) — both are unplayable, and each one left in
            // would stall the rotation while mpv discovers that for itself.
            if (id.isEmpty() || title.isEmpty()
                || title == QLatin1String("[Deleted video]")
                || title == QLatin1String("[Private video]")
                || title == QLatin1String("[Unavailable video]"))
                continue;
            const QString seenKey = source.kind + ':' + id;
            if (m_seen.contains(seenKey))
                continue;
            m_seen << seenKey;

            QString channel = obj.value(QLatin1String("channel")).toString();
            if (channel.isEmpty())
                channel = obj.value(QLatin1String("uploader")).toString();
            const auto [artist, song] = parseArtistSong(title, channel);

            QVariantMap item;
            item["id"]       = id;
            // The overlay script tells the two apart by this: a bumper gets
            // neither title card nor screen bug, however cleanly its title parses.
            item["kind"]     = source.kind;
            item["url"]      = QStringLiteral("https://www.youtube.com/watch?v=") + id;
            item["title"]    = title;
            item["artist"]   = artist;
            item["song"]     = song;
            item["album"]    = QString();
            item["label"]    = QString();
            item["director"] = QString();
            item["duration"] = obj.value(QLatin1String("duration")).toDouble();
            // musictv_metadata.json wins over anything parsed
            const QJsonObject over = m_overrides.value(id).toObject();
            for (auto it = over.begin(); it != over.end(); ++it)
                if (item.contains(it.key()))
                    item[it.key()] = it.value().toVariant();

            (source.kind == QLatin1String("music") ? m_music : m_bumpers).append(item);
            ++added;
        }
        qDebug("[MusicTV] %s: %d videos", qPrintable(source.url), added);
        fetchNext();
    };
    connect(proc, &QProcess::finished, this, finish);
    // finished() is never emitted when the binary fails to launch
    connect(proc, &QProcess::errorOccurred, this,
            [finish](QProcess::ProcessError processError) {
                if (processError == QProcess::FailedToStart)
                    finish();
            });
    QTimer::singleShot(kFetchTimeoutMs, proc, [proc]() { proc->kill(); });
    proc->start(ytdlp::locate(m_dataRoot), args);
}

void MusicTvBackend::finishRefresh() {
    m_refreshing = false;

    if (m_music.isEmpty()) {
        // Keeping a previous good cache beats replacing it with an empty one:
        // a transient network failure would otherwise take the channel off air.
        if (!emitFromCache())
            emit errorOccurred(QStringLiteral("COULD NOT LOAD MUSIC VIDEOS\n"
                                              "CHECK YOUR NETWORK CONNECTION AND THAT\n"
                                              "YT-DLP IS INSTALLED AND UP TO DATE"));
        return;
    }

    QJsonObject cache;
    cache["generated"] = QDateTime::currentSecsSinceEpoch();
    cache["music"]     = QJsonArray::fromVariantList(m_music);
    cache["bumpers"]   = QJsonArray::fromVariantList(m_bumpers);
    QSaveFile out(path(kCacheFileName));
    if (out.open(QIODevice::WriteOnly)) {
        out.write(QJsonDocument(cache).toJson(QJsonDocument::Indented));
        if (!out.commit())
            qWarning("[MusicTV] could not write %s", kCacheFileName);
    }
    qDebug("[MusicTV] cache rebuilt: %lld music, %lld bumpers",
           qlonglong(m_music.size()), qlonglong(m_bumpers.size()));

    emit poolsLoaded(m_music, m_bumpers);
}

#pragma once

#include <QJsonObject>
#include <QObject>
#include <QString>
#include <QStringList>
#include <QVariant>
#include <QVariantList>

class QProcess;

// Backend for the Music TV module.
//
// The user lists YouTube playlists (or single videos) in two files in dataRoot:
//   musictv_playlists.txt — music videos
//   musictv_bumpers.txt   — bumpers/idents
// Same line format as youtube_playlists.txt: one URL or ID per line, `#`
// comments, optional "Name | <url>" prefix (the name is not used).
//
// Each entry is listed with yt-dlp --flat-playlist (a playlist's RSS feed stops
// at 15 entries — too few for a rotation), artist/song are parsed from each
// title, and the result is written to musictv_cache.json. That file stays on
// disk because scripts/musictv-overlay.lua reads it inside mpv for the title
// cards. Optional musictv_metadata.json (keyed by video id) overrides any field.
//
// load() rebuilds the cache only when it is missing, older than an input file,
// or older than kMaxCacheAgeSecs (to drop videos YouTube has taken down).
// Otherwise the cached pools are served immediately.
class MusicTvBackend : public QObject {
    Q_OBJECT
public:
    explicit MusicTvBackend(const QString &appRoot, const QString &dataRoot,
                            QObject *parent = nullptr);

    // Emits poolsLoaded or errorOccurred. While a rebuild is in flight a second
    // call just waits on it.
    Q_INVOKABLE void load();

    // The data dir the backend reads and writes, so the overlay script is handed
    // the same paths even under a DATA_ROOT override.
    Q_INVOKABLE QString data_root() const { return m_dataRoot; }

signals:
    // Fired when a rebuild starts, so the view can say what the wait is for.
    void refreshStarted();
    void poolsLoaded(const QVariant &music, const QVariant &bumpers);
    void errorOccurred(const QString &message);

private:
    struct Source {
        QString url;     // canonical playlist or watch URL
        bool    isPlaylist = true;
        QString kind;    // what the videos ARE: "music" or "bumper"
    };

    QString     path(const char *fileName) const;
    bool        cacheIsStale() const;
    bool        emitFromCache();
    QList<Source> readSources(const char *fileName, const QString &kind) const;
    void        fetchNext();
    void        finishRefresh();

    QString m_appRoot;
    QString m_dataRoot;

    // Refresh state: sources waiting to be listed, and the pools built so far.
    QList<Source> m_queue;
    QVariantList  m_music;
    QVariantList  m_bumpers;
    QStringList   m_seen;          // video ids already pooled (per kind)
    QJsonObject   m_overrides;     // musictv_metadata.json
    bool          m_refreshing = false;

    static constexpr int    kMaxPlaylistItems = 500;
    static constexpr int    kFetchTimeoutMs   = 120000;
    static constexpr qint64 kMaxCacheAgeSecs  = 24 * 60 * 60;
};

-- musictv-overlay.lua — title card + corner screen bug, drawn inside mpv.
--
-- Loaded by the Music TV module (modules/musictv/views/Player.qml) with --script=, and
-- configured with --script-opts-append (never --script-opts: MpvController
-- builds its own --script-opts and a second flag would replace it, not append).
--
-- Options (all prefixed musictv-):
--   musictv-cache=<path>   musictv_cache.json written by MusicTvBackend — supplies the
--                      artist/song/album/label/director lines, keyed by video id
--   musictv-bugs=<dir>     directory of pre-converted .bgra bugs (see musictv_bugs.py)
--   musictv-titles=0|1     title card on/off
--   musictv-bug=0|1        screen bug on/off
--
-- Why the card is drawn here and not in QML: on the Pi mpv takes the VT and the
-- DRM master while it plays, so nothing Qt draws is on screen during playback.
--
-- The card uses its own osd-overlay, NOT mp.set_osd_ass(), which the app's OSC
-- (scripts/mpv-osc.lua) owns — the two would clobber each other.

local utils  = require 'mp.utils'
local msg    = require 'mp.msg'

local opts       = mp.get_property_native("options/script-opts") or {}
local CACHE_PATH = opts["musictv-cache"] or ""
local BUG_DIR    = opts["musictv-bugs"]  or ""
local SHOW_TITLE = (opts["musictv-titles"] or "1") ~= "0"
local SHOW_BUG   = (opts["musictv-bug"]    or "1") ~= "0"
-- Josefin Sans is the shipped default: a geometric sans based on Rudolf Koch's
-- Kabel (1927), the lettering these title cards imitate, and licensed under the
-- SIL OFL so it can be redistributed with the module. SemiBold rather than
-- Regular because Regular is too light to hold up over moving video.
-- Install it into the user font directory (fc-cache). libass falls back
-- silently when a family is missing, so musictv-font names a substitute (e.g.
-- "Kabel" if you have licensed it) rather than editing this file.
local FONT       = opts["musictv-font"] or "Josefin Sans SemiBold"

-- Timing (seconds)
local START_AT   = 2.0    -- card appears this long after the video starts
local HOLD       = 4.0    -- fully-visible time
local FADE       = 0.5
local END_BEFORE = 10.0   -- second showing starts this long before the end
local MIN_DUR_FOR_END = 25.0

local BUG_ID     = 63     -- overlay id, high enough to stay clear of other scripts

-- Layout, as fractions of the OSD height/width. Card and bug stay inside the
-- broadcast title-safe area (10% in from each edge), because a CRT overscans
-- and composite video fills the whole raster: at the old 5-7.5% margins both
-- were partly cut off on a real set.
local L_MARGIN   = 0.10
local BLOCK_BOT  = 0.90   -- bottom of the text block in normal playback
local BLOCK_BOT_OSC = 0.60 -- ... and when the OSC menu is up (its bar sits at 0.74)
-- Josefin Sans has a notably small x-height, so the same nominal size reads
-- smaller than a grotesque would; measured against video on a 480-line screen,
-- this is where lowercase stops being marginal.
local FONT_SIZE  = 0.056
local BUG_HEIGHT = 0.10
local BUG_MARGIN_X = 0.10
local BUG_MARGIN_Y = 0.10

math.randomseed(os.time())

-- ---------------------------------------------------------------------------
-- Metadata cache
-- ---------------------------------------------------------------------------

local meta_by_id = {}

local function load_cache()
    if CACHE_PATH == "" then return end
    local f = io.open(CACHE_PATH, "r")
    if not f then
        msg.warn("no cache at " .. CACHE_PATH)
        return
    end
    local text = f:read("*a")
    f:close()
    local data = utils.parse_json(text or "")
    if not data then
        msg.warn("cache is not valid JSON")
        return
    end
    local n = 0
    for _, list in ipairs({ data.music or {}, data.bumpers or {} }) do
        for _, entry in ipairs(list) do
            if entry.id then
                meta_by_id[entry.id] = entry
                n = n + 1
            end
        end
    end
    msg.info("loaded metadata for " .. n .. " videos")
end

-- The playlist entries are YouTube URLs, so the id is in the URL mpv was given.
-- (ytdl_hook resolves the media URL behind the scenes but leaves `path` alone.)
local function video_id(path)
    if not path then return nil end
    return path:match("[?&]v=([%w_%-]+)")
        or path:match("youtu%.be/([%w_%-]+)")
        or path:match("/shorts/([%w_%-]+)")
end

-- ---------------------------------------------------------------------------
-- Title card
-- ---------------------------------------------------------------------------

local overlay      = mp.create_osd_overlay("ass-events")
local card_lines   = nil    -- { {text=, italic=}, ... } for the current file
local shown_start  = false
local shown_end    = false
local anim         = nil    -- { t0 = <mp.get_time()>, osc = <bool> }
local anim_timer   = nil

local function ass_escape(s)
    return tostring(s):gsub("\\", "\\\\"):gsub("{", "\\{"):gsub("}", "\\}")
end

-- Bumpers parse into artist/song as readily as anything else ("Jim Paul" /
-- "MTV Promo 1981"), so the on-screen furniture is gated on what the video *is*,
-- not on whether a title happened to parse. An entry missing from the cache is
-- treated as not-music: the rotation only ever plays cached entries, so an
-- unknown file is something unexpected rather than a music video.
local function is_music(m)
    return m ~= nil and m.kind ~= "bumper"
end

local function build_lines(m)
    if not is_music(m) then return nil end
    local artist = m.artist or ""
    local song   = m.song or ""
    if artist == "" and song == "" then return nil end   -- bumpers, idents, etc.

    local lines = {}
    if artist ~= "" then lines[#lines + 1] = { text = artist, italic = false } end
    if song   ~= "" then lines[#lines + 1] = { text = '"' .. song .. '"', italic = true } end
    if m.album    and m.album    ~= "" then lines[#lines + 1] = { text = m.album,    italic = true  } end
    if m.label    and m.label    ~= "" then lines[#lines + 1] = { text = m.label,    italic = false } end
    if m.director and m.director ~= "" then lines[#lines + 1] = { text = "Director: " .. m.director, italic = false } end
    return lines
end

local function hide_card()
    overlay.data = ""
    overlay:update()
end

-- alpha: 0 = invisible, 1 = fully opaque
local function draw_card(alpha, osc)
    local ww, wh = mp.get_osd_size()
    if not ww or ww == 0 or wh == 0 or not card_lines then return end

    local fs   = math.floor(wh * FONT_SIZE)
    local lh   = math.floor(fs * 1.18)
    local x    = math.floor(ww * L_MARGIN)
    local bot  = math.floor(wh * (osc and BLOCK_BOT_OSC or BLOCK_BOT))
    local top  = bot - lh * (#card_lines - 1)
    local a    = string.format("&H%02X&", math.floor((1 - alpha) * 255 + 0.5))

    local events = {}
    for i, line in ipairs(card_lines) do
        events[#events + 1] = string.format(
            -- No \b1: the weight is chosen by picking the family (SemiBold by
            -- default). Bold on top of that is libass's synthetic emboldening,
            -- which thickens geometric strokes into something the typeface
            -- never had. The drop shadow is what carries legibility over
            -- video, exactly as it did on air.
            "{\\an1\\pos(%d,%d)\\fn%s\\fs%d\\b0\\i%d\\1c&HFFFFFF&\\alpha%s\\bord0\\shad2\\4c&H000000&\\4a&H40&}%s",
            -- \an1 anchors each line by its BOTTOM-left, so the y passed here is
            -- where that line ends. Adding fs (as this did) pushed the block a
            -- whole line-height below BLOCK_BOT, which is why raising FONT_SIZE
            -- for Josefin also dragged the card down the screen.
            x, top + lh * (i - 1), FONT, fs, line.italic and 1 or 0, a,
            ass_escape(line.text))
    end

    overlay.res_x = ww
    overlay.res_y = wh
    overlay.data  = table.concat(events, "\n")
    overlay:update()
end

local function stop_anim()
    if anim_timer then anim_timer:kill(); anim_timer = nil end
    anim = nil
    hide_card()
end

-- One showing: fade in, hold, fade out. Re-triggering restarts it from the top
-- (the OSC can pop the card while it is already on screen).
local function show_card(osc)
    if not SHOW_TITLE or not card_lines then return end
    anim = { t0 = mp.get_time(), osc = osc or false }
    if not anim_timer then
        anim_timer = mp.add_periodic_timer(1 / 30, function()
            if not anim then return end
            local t = mp.get_time() - anim.t0
            local total = FADE + HOLD + FADE
            if t >= total then
                stop_anim()
            elseif t < FADE then
                draw_card(t / FADE, anim.osc)
            elseif t < FADE + HOLD then
                draw_card(1, anim.osc)
            else
                draw_card((total - t) / FADE, anim.osc)
            end
        end)
    end
    draw_card(0, anim.osc)
end

-- ---------------------------------------------------------------------------
-- Screen bug
-- ---------------------------------------------------------------------------

local bug_file, bug_w, bug_h = nil, 0, 0
local bug_placed = false

-- Filename carries the pixel size — Lua cannot read a PNG, and overlay-add
-- needs w/h/stride up front. musictv_bugs.py writes <name>.<w>x<h>.bgra.
local function pick_bug(m)
    bug_file, bug_w, bug_h, bug_placed = nil, 0, 0, false
    if not SHOW_BUG or BUG_DIR == "" then return end
    -- Bumpers carry the channel's own branding; a bug on top of an ident is a
    -- bug on top of a bug. Same gate as the title card: furniture belongs on
    -- the music videos only.
    if not is_music(m) then return end
    local files = utils.readdir(BUG_DIR, "files")
    if not files then return end
    local usable = {}
    for _, name in ipairs(files) do
        local w, h = name:match("%.(%d+)x(%d+)%.bgra$")
        if w then usable[#usable + 1] = { name = name, w = tonumber(w), h = tonumber(h) } end
    end
    if #usable == 0 then return end
    local pick = usable[math.random(#usable)]
    bug_file, bug_w, bug_h = BUG_DIR .. "/" .. pick.name, pick.w, pick.h
end

local function place_bug()
    if bug_placed or not bug_file then return end
    local ww, wh = mp.get_osd_size()
    if not ww or ww == 0 or wh == 0 then return end   -- no video sized yet

    local dh = math.floor(wh * BUG_HEIGHT)
    local dw = math.floor(dh * bug_w / bug_h)
    local x  = ww - dw - math.floor(ww * BUG_MARGIN_X)
    local y  = wh - dh - math.floor(wh * BUG_MARGIN_Y)

    mp.commandv("overlay-add", BUG_ID, x, y, bug_file, 0, "bgra",
                bug_w, bug_h, bug_w * 4, dw, dh)
    bug_placed = true
end

local function remove_bug()
    if bug_placed then
        mp.commandv("overlay-remove", BUG_ID)
        bug_placed = false
    end
end

-- ---------------------------------------------------------------------------
-- Per-file wiring
-- ---------------------------------------------------------------------------

mp.register_event("file-loaded", function()
    stop_anim()
    remove_bug()
    shown_start, shown_end = false, false
    local m = meta_by_id[video_id(mp.get_property("path")) or ""]
    card_lines = build_lines(m)
    pick_bug(m)
end)

mp.register_event("end-file", function()
    stop_anim()
    remove_bug()
end)

-- Scheduler. A poll (rather than time-pos observers) so a seek past a trigger
-- point behaves sanely: each showing fires at most once per file either way.
mp.add_periodic_timer(0.25, function()
    place_bug()

    if not SHOW_TITLE or not card_lines then return end
    local pos = mp.get_property_number("time-pos")
    if not pos then return end

    if not shown_start and pos >= START_AT then
        shown_start = true
        show_card(false)
        return
    end
    local dur = mp.get_property_number("duration")
    if not shown_end and dur and dur > MIN_DUR_FOR_END and pos >= dur - END_BEFORE then
        shown_end = true
        show_card(false)
    end
end)

-- The app's OSC broadcasts this when its menu opens (it tells the volume bar to
-- stand down). Re-show the card with the on-screen controls, drawn higher up so
-- it does not land on top of the seek bar.
mp.register_script_message("240mp-osd-volume-hide", function()
    show_card(true)
end)

load_cache()

-- hdhr-crop.lua — per-channel crop memory for the HDHomeRun module.
--
-- Crop is mpv's `panscan` property: the OSC's CROP button runs
-- `cycle-values panscan 0 1` (scripts/mpv-osc.lua). This script applies the
-- value remembered for the channel being tuned, then remembers whatever the
-- user leaves it on.
--
-- Why this lives in mpv rather than in QML, which owns everything else in this
-- module: QML can do neither half. It cannot read an mpv property (MpvController
-- exposes no getter and its sendCommand is private, not Q_INVOKABLE), so it
-- cannot see the user press CROP; and it cannot write a file at any setting, so
-- it could not persist the answer if it had one. Inside mpv both are three lines.
--
-- Loaded by views/Player.qml via --script=, configured with
-- --script-opts-append= (never --script-opts: MpvController has already built
-- that list, and a second one replaces it).
--
--   hdhr-crop-store=<path>     the memory file
--   hdhr-crop-channel=<num>    virtual channel being tuned, e.g. 12.1

local msg = require "mp.msg"
local opt = require "mp.options"

local opts = {
    ["hdhr-crop-store"] = "",
    ["hdhr-crop-channel"] = "",
}
opt.read_options(opts, "hdhr-crop")

-- Set by MpvController on decode paths where --panscan blanks the video (the
-- Pi 3 overlay path with 1080p Playback ON). The app hides the CROP button
-- there; forcing panscan ourselves would black out the picture, so this script
-- stands down entirely. Same option the OSC reads.
local crop_unavailable = mp.get_opt("hide-crop") == "1"

local store = opts["hdhr-crop-store"]
local channel = opts["hdhr-crop-channel"]

-- channel -> "0" / "1". Plain text, one "channel<TAB>value" per line: the whole
-- payload is a number per channel, and this stays readable and hand-editable
-- without depending on a JSON serialiser being present in every mpv build.
local memory = {}
local applied = nil

local function load()
    local fh = io.open(store, "r")
    if not fh then return end
    for line in fh:lines() do
        local key, value = line:match("^(%S+)%s+(%d)%s*$")
        if key then memory[key] = value end
    end
    fh:close()
end

local function save()
    if store == "" then return end
    -- Write-and-rename so a power cut mid-write cannot leave a truncated file:
    -- this box is switched off at the wall, by design.
    local tmp = store .. ".tmp"
    local fh = io.open(tmp, "w")
    if not fh then
        msg.warn("cannot write " .. tmp)
        return
    end
    local keys = {}
    for key in pairs(memory) do keys[#keys + 1] = key end
    table.sort(keys)
    for _, key in ipairs(keys) do
        fh:write(string.format("%s\t%s\n", key, memory[key]))
    end
    fh:close()
    os.rename(tmp, store)
end

local function remember(value)
    if channel == "" or memory[channel] == value then return end
    memory[channel] = value
    save()
    msg.info(string.format("channel %s crop %s", channel, value))
end

if crop_unavailable or store == "" or channel == "" then
    msg.verbose("inactive (crop unavailable or no channel/store given)")
else
    load()

    -- Apply on file-loaded, not now: panscan set before the file is open does
    -- not survive the load, and this is also when the video's own properties
    -- become real.
    mp.register_event("file-loaded", function()
        local want = memory[channel]
        if want == nil then return end     -- never cropped here; leave the app's
                                           -- own auto_crop default alone
        applied = want
        mp.set_property("panscan", want == "1" and "1.0" or "0.0")
        msg.info(string.format("channel %s restored crop %s", channel, want))
    end)

    -- Observing the property rather than intercepting the CROP button means this
    -- also catches a crop set any other way, and cannot drift out of step with
    -- what the picture is actually doing.
    mp.observe_property("panscan", "number", function(_, value)
        if value == nil then return end
        local now = value > 0.5 and "1" or "0"
        -- Ignore the echo of our own restore.
        if applied ~= nil and now == applied then
            applied = nil
            return
        end
        applied = nil
        remember(now)
    end)
end

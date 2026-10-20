**Open as:** an issue on `anthonycaccese/240-MP` first, to agree the shape before
any code. The PR follows once the design is settled.

**Issue title:** Proposal: let a module run a small background component that
follows what's on screen

**Future branch:** `factorsparsity:app-hook` → `anthonycaccese/240-MP:main`

---

## The problem

I built a front panel for my 240-MP box: a small OLED (SSD1306) wired to the
Pi's I2C pins, styled as a VCR display. It shows the clock when idle, the
highlighted menu item while browsing, the elapsed time during playback and, in
the HDHomeRun module, the channel number.

Everything except one piece fits inside a normal module. The panel needs to
know **what's on screen**, and nothing in a module outlives a screen change:

- a module's QML only exists while that module is open
- `AppCore` has no "current screen" property or signal
- navigation is the QML stack around `moduleLoader` in `Main.qml`, so
  `moduleLoader.source` is the only thing that knows

Today I patch `Main.qml` at install time with a script, which inserts a
`Loader` for my component and an `onSourceChanged` line. It works, but every
240-MP update overwrites it, and it can't be offered to anyone else in that
form.

## Proposal

A module can declare one optional non-visual QML component that `Main.qml`
loads at startup and keeps alive for the whole session. Main.qml exposes what's
on screen as two read-only properties that component can bind to.

**1. New optional manifest key**

```json
{
  "id": "com.240mp.segdisplay",
  "background_qml": "views/Background.qml"
}
```

**2. Two properties on `Main.qml`'s root**

```qml
// What the user is looking at, for background components (and anything else).
readonly property url  currentView:     moduleLoader.source
readonly property Item currentViewItem: moduleLoader.item
```

**3. One `Instantiator` in `Main.qml`**

It loads each enabled module's `background_qml`. Because it is a `Loader` inside
`Main.qml`, the component resolves `root.*` exactly as views do, and it already
has `mpvController` and `appCore` as context properties. The only C++ change is
one more field in each entry `get_installed_modules()` returns: `background_qml`,
resolved to an app-relative path the same way the entry point is (empty when
the manifest has none). Each entry already carries `enabled`.

```qml
Instantiator {
    model: appCore.get_installed_modules().filter(function(m) {
        return m.enabled && m.background_qml
    })
    delegate: Loader { source: modelData.background_qml }
}
```

Roughly 15 lines in `Main.qml`, a few in `AppCore` and a table row in
ARCHITECTURE.md. No existing module changes.

## What it's for

- A front panel or any auxiliary display: OLED, 7-segment, a small LCD.
- Other "always on" behaviour that today would need a `Main.qml` edit: a
  playback log or scrobbler, a sleep timer, an activity LED, an idle action.

## What it deliberately isn't

- **Not visual.** A background component draws nothing in the app window. Asking
  for an overlay would be a different and bigger proposal.
- **Not a key handler.** Input stays with views, as now.
- **No new permissions.** It runs with the same access any module view already
  has.

## Questions for you

1. Is a manifest key the right switch, or would you prefer an explicit list in
   config?
2. Are `currentView` / `currentViewItem` acceptable names on `root`, or is there
   a convention I should follow?
3. Is a field on `get_installed_modules()` fine, or would you rather have a
   separate call that returns just the background components?
4. Should a disabled module's component be unloaded live when it's toggled off,
   or is restart-to-apply fine?

## After this lands

The front panel module would follow as its own PR, built on this hook with no
`Main.qml` patch. It currently lives on `factorsparsity:frontpanel-module`
(`modules/segdisplay`: the QML hook, a Python display daemon and a systemd
unit).

## AI involvement

This proposal was drafted with Claude Code, and I edited it. The front panel
module it comes from was also written with Claude Code, and I tested it on my
own hardware.

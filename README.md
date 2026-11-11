# Screen Light

Make your Tuya / Smart Life RGB bulbs (Wi-Fi, or Bluetooth on Linux) follow the colours on your screen.

Pick a display, and Screen Light keeps averaging its colours and sends the result to your bulbs over your
local network (no cloud round-trips, so it's quick). With several bulbs, each one follows its own part of the screen:

| Bulbs | Screen regions (in list order)                          |
|-------|---------------------------------------------------------|
| 1     | whole screen                                            |
| 2     | left half, right half                                   |
| 3     | left, middle, right thirds                              |
| 4     | top-left, top-right, bottom-left, bottom-right quadrants|
| 5+    | equal vertical strips, left to right                    |

Works on **Windows 10/11** and **Linux (X11)**.

## Install and run

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/):
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. In this folder, run:

   ```
   uv run screenlight
   ```

   The first run downloads a suitable Python and the dependencies (`mss`, `numpy`, `tinytuya`).
   uv uses its own Python builds, which always include Tk, so you don't need `python3-tk` on Linux.

## First-time bulb setup

Tuya bulbs only accept local commands encrypted with a per-device **local key**. You fetch the keys once from
Tuya's developer cloud. After that, everything runs locally.

1. Pair the bulbs in the **Smart Life** or **Tuya Smart** app as usual.
2. Create a free account at [iot.tuya.com](https://iot.tuya.com), then go to **Cloud → Development → Create Cloud
   Project**. Choose *Smart Home*, and pick the data centre that matches your app account's region.
3. In the project, open **Devices → Link App Account** and scan the QR code with the app (Me → scan icon).
4. Copy the **Access ID** and **Access Secret** from the project overview.
5. In Screen Light, click **Find bulbs...**, paste the ID and secret, pick the same data centre, and click
   **Fetch & scan**. This takes about 20 s while it listens for bulbs on your network. Tick the bulbs you want and click **Add selected**.

The cloud credentials aren't saved. There's a more detailed walkthrough with screenshots in the
[tinytuya guide](https://github.com/jasonacox/tinytuya#setup-wizard---getting-local-keys).

If you already have device IDs and local keys (for example from `python -m tinytuya wizard`), use
**Add manually...** instead.

## Using it

- **Display**: the monitor to follow. The preview shows that screen, each bulb's region and the colour it gets.
- **Bulbs list**: the order decides the region. Use **▲ Up / ▼ Down** to rearrange, and click the checkbox to enable or disable
  a bulb. **Flash** blinks a bulb red so you can tell which physical lamp it is.
- **Start / Stop**: starts or stops syncing. With no bulbs enabled, Start just runs the live preview.
- **Mode**: *Vibrant* (default) favours saturated pixels and ignores black bars, so a scene with a little
  colour doesn't turn into murky grey. *Plain average* is the straight mean of the region.
- **Saturation boost**: makes the bulbs' colours more vivid.
- **Smoothing**: higher values give calmer, slower fades. 0 reacts instantly.
- **Min / Max brightness**: bulb brightness follows how bright the region is, scaled into this range.
- **Bulb updates / s**: caps how often each bulb is sent a new colour. Most Tuya bulbs handle 5–10 per second.
- **Follow screen / Manual colours** (top left): in *Manual colours* mode the bulbs show colours you choose
  instead of the screen. The preview turns into one tile per bulb.
  - Select a bulb and click **Colour...**, or click its tile, to pick its colour. Changes apply straight away, even
    while running.
  - **Apply gradient** spreads a preset (Sunset, Ocean, Forest, Fire, Aurora, Candy, Rainbow, Warm white) across the
    enabled bulbs in list order. Tweak single bulbs afterwards if you like.
  - *Max brightness* dims manual colours, and *Smoothing* fades between them. Your colours are remembered.
- **When stopped**: what the bulbs do when you stop or close the app: keep the last colour, go warm white, or turn off.

Settings are saved automatically to `~/.config/screenlight/config.json` on Linux and
`%APPDATA%\screenlight\config.json` on Windows. That file contains your bulbs' local keys, so it's written
readable only by you.

## Bluetooth bulbs (Linux)

Screen Light can also drive Tuya / Smart Life **Bluetooth beacon** bulbs, the kind that pair straight to your phone
over Bluetooth (no hub) and always show as offline in the Tuya cloud, such as "Smart BT" bulbs. Smart Life controls
them by broadcasting short encrypted messages, and Screen Light sends the same messages from your computer's
Bluetooth adapter.

**Setup (once per bulb):**

1. Run **Find bulbs...** as above. Bluetooth bulbs show as *not seen (Auto)*.
2. Click the bulb's row, then **Set up as Bluetooth...**.
3. Press **Listen** and enter your password when asked. Then, with your phone's Bluetooth on and the phone near
   the computer, turn the bulb **off, then on** in Smart Life. Screen Light picks up those two commands and works
   out the bulb's Bluetooth key from them.
4. When it says *Found …*, click **Add**, then **Add selected**. The bulb appears with `Bluetooth` as its IP.

**Using them:**

- When you press Start (or Flash) with a Bluetooth bulb enabled, Linux asks for your password once per session.
  Sending these broadcasts needs root, so a small helper (`src/screenlight/beacon/helper.py`) runs as root until you
  close the app. It only ever receives finished broadcasts, never your keys.
- Bluetooth bulbs never reply, so their status shows `sending` rather than `online`.
- Up to 3 Bluetooth bulbs can sync at once. Your phone keeps working alongside Screen Light.
- Colours are corrected for these bulbs' LEDs (`BEACON_HUE_POINTS` in `colour.py`). To re-tune for your bulbs, run
  `uv run python tools/beacon_calibrate.py --local-key … --beacon-key … --node …`.

**Bluetooth status messages:**

| Status | What to do |
|---|---|
| `no permission` | The password dialog was cancelled. Press Start again. |
| `Bluetooth off` | Turn Bluetooth on. Syncing resumes by itself. |
| `Bluetooth adapter needs a reset: toggle Bluetooth off/on` | Turn Bluetooth off and on again. |
| `run Bluetooth setup again` | The bulb was re-paired in Smart Life, so its keys changed. Run Find bulbs and set it up again. |
| `too many Bluetooth bulbs` | Only 3 can sync at once (fewer if another program is advertising). |
| `helper stopped` | The root helper crashed twice. Restart Screen Light. |
| `Bluetooth bulbs need Linux` | Bluetooth bulbs aren't supported on Windows. |

**Security note:** the root helper runs from this folder, which your user account can write to. A program running as
you could change `helper.py` before you type your password. For a personal tool that's an accepted trade-off. If it
matters to you, copy the project somewhere only root can write to.

## Troubleshooting

- **Linux shows a Wayland warning / black preview**: screen capture needs an X11 session. Log out and choose
  the X11/Xorg session (for example "Cinnamon" rather than "Cinnamon (Wayland)", or "Ubuntu on Xorg").
- **Windows firewall prompt**: allow Python on private networks. Bulb discovery listens for UDP broadcasts on
  ports 6666, 6667 and 7000.
- **Bulb shows `offline`**:
  - Its IP address may have changed. Run **Find bulbs...** again, or give the bulb a fixed DHCP
    reservation in your router. An IP of `Auto` finds the bulb by broadcast each time it connects.
  - Some bulbs only allow one or two local connections. Other local controllers, such as Home Assistant
    LocalTuya, can use them up.
  - If you re-paired the bulb in the app, its local key changed. Run **Find bulbs...** again.
- **"bulb has no colour mode"**: the device is a white-only bulb or a dimmer.
- **Find bulbs fails with a sign/permission error**: check that the data centre matches your app account and that the
  app account is linked to the cloud project. The Tuya IoT Core trial must also be active (you can extend it for free in the
  project's Service API page). Local control keeps working after the trial ends. Only fetching keys needs the cloud.

## Development

```
uv run pytest          # unit tests (+ a capture test when a display is available)
uv run screenlight     # run the app
```

```
src/screenlight/
  regions.py    split the frame into per-bulb regions
  colour.py     region pixels -> HSV colour (vibrant / plain), smoothing
  capture.py    monitor list + downsampled screen grabs (mss)
  bulbs.py      one worker thread per bulb (tinytuya), latest-colour-wins, reconnects
  discovery.py  Tuya cloud key fetch + LAN scan, merged into bulb entries
  beacon/       Bluetooth beacon bulbs: protocol.py (P2 codec), helper.py (root advertiser),
                radio.py (helper lifecycle, slots, sequence numbers)
  engine.py     capture -> colours -> bulbs loop
  config.py     settings + bulb list (JSON)
  gui/          tkinter main window and dialogs
```

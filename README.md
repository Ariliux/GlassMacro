<p align="center">
  <img src="docs/icon.png" width="96" alt="GlassMacro icon">
</p>

<h1 align="center">GlassMacro</h1>

<p align="center">An AFK helper for <b>Roblox Rivals</b> Free For All, for racking up playtime.</p>

<p align="center">
  <img src="docs/screenshot.png" width="420" alt="GlassMacro window">
</p>

## What it does

- **Picks your loadout:** Grenade Launcher first, then Random for the other three slots, every time the weapon picker opens.
- **Keeps playing:** between picks it jumps and fires, so it respawns by itself and skips the end-of-round screens.
- **Gets back into games:**
  - from the hub, it walks the menu into Free For All
  - from spectating, it presses Join
  - after a disconnect, it presses Reconnect, and if that fails it restarts Roblox and rejoins.
- **Keeps Roblox fullscreen:** it presses F11 if Roblox is in a window.
- **Only acts while Rivals is the window in front.** If you click away, it pauses until you come back.

## What you need

- Windows 10 or 11
- A **1920×1080** screen. Other resolutions aren't supported yet, and the app warns you if yours is different.
- Roblox Rivals

## Install

1. Go to **[Releases](../../releases/latest)** and download `GlassMacro-v1.2.0.zip`.
   **Only download it from here.** Don't use copies other people re-upload.
2. Right-click the zip, choose **Extract All**, then open the folder and run **`GlassMacro.exe`**.
   Don't run it from inside the zip.
3. Windows will probably warn you the first time. See
   [If Windows or your antivirus warns you](#if-windows-or-your-antivirus-warns-you) below.

Your settings are saved in `%LOCALAPPDATA%\GlassMacro`. To uninstall, delete the
GlassMacro folder and that one.

## First-time setup (about 30 seconds)

GlassMacro needs to learn where your weapons are. Nothing else needs setting up.

1. In Rivals, open the **weapon picker**.
2. In GlassMacro, press **Set up**.
3. For each item below, **hover** over it and press **F8**. Don't click.
   1. the **Random** tile
   2. the **Grenade Launcher's name**. Hover the label, not the picture.
   3. the **first loadout slot** along the top

Press **Esc** at any point to cancel. Redo this with **Recalibrate** if an event adds or
removes weapons and the grid moves.

## Using it

- Press **Start** in the app, or **F8** inside Rivals. **F8** again stops it.
- You can start from the hub, mid-match or while spectating. It works out where you are.
- The status card shows what it's doing right now, how long it has been running, how
  many loadouts it has picked and how many times it rejoined.
- **Settings:**
  - Turn off **Keep Roblox fullscreen**, **Reconnect automatically** or
    **Save pictures of what it clicks**.
  - **Test** the detection threshold.
  - **Open the data folder**, where the log and pictures are kept.

## If Windows or your antivirus warns you

GlassMacro isn't code-signed, because signing costs money every year. Windows is careful
with any new app that hasn't been signed, so a warning is normal.

**"Windows protected your PC"**: click **More info**. Check that it says
`GlassMacro.exe`, then click **Run anyway**. That allows only this file.

**Your browser says the file "isn't commonly downloaded"**: choose **Keep**. In Edge,
it's **…** → **Keep** → **Show more** → **Keep anyway**.

**"Smart App Control blocked this app"**: there's no Run anyway button for this one.
The only way around it is to turn Smart App Control off, which lowers your PC's
protection. That's your call, and skipping GlassMacro is completely fine.

**Defender or another antivirus quarantines it**: don't restore it. Open an
[issue](../../issues) with the exact detection name, so it can be reported to that
antivirus as a false positive.

**Never** turn off Defender or real-time protection, and never add exclusions for whole
folders like Downloads to make any program run. GlassMacro doesn't need that, and
nothing you download ever should.

## Good to know

- **Use it at your own risk.** Automating a game can go against Roblox's or the game's
  rules, and your account is your responsibility.
- **Not affiliated with Roblox or Rivals.**
- **What it does on your PC:**
  - It reads the screen to see what's showing, and uses the keyboard and mouse only
    inside Rivals.
  - It listens for F8 to start and stop, plus Esc during setup. It doesn't record
    anything else you type.
  - It only closes and relaunches Roblox itself.
  - Nothing is ever sent anywhere, and the pictures it saves stay in your data folder.

## Building it yourself

Needs Python 3.14 on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\pyinstaller --noconfirm --clean --onedir --windowed --noupx --name GlassMacro `
  --icon glassmacro.ico --add-data "glassmacro.ico;." --version-file version_info.txt glassmacro.py
```

The app ends up in `dist\GlassMacro\`. `python make_icon.py` redraws the icon.
`python test_detectors.py` checks the screen detectors against real screenshots, which
you supply yourself because they aren't in the repo.

## License

[MIT](LICENSE)

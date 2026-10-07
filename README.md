<p align="center">
  <img src="docs/icon.png" width="96" alt="GlassMacro icon">
</p>

<h1 align="center">GlassMacro</h1>

<p align="center">
  <img src="docs/screenshot.png" width="420" alt="GlassMacro window">
</p>

## Why I made this

I wanted the **Glass wrap** in Rivals, and grinding FFA playtime by hand takes forever. I
tried TinyTask first, but it kept picking the wrong weapon, because the weapon grid moves
around whenever there's an event. So I built my own macro that actually *looks* at the
screen instead of clicking fixed spots.

It got me the wrap, and i left it running for 10+ hours straight with no issues whatsoever. 

## What it does

- **Picks your loadout.** Every time the weapon picker opens, it goes **Grenade Launcher**,
  then **Random** for the other 3 slots.
- **Keeps playing.** Between rounds it jumps and fires, so it respawns by itself and skips
  the end screens.
- **Gets back into games on its own:**
  - Sitting in the hub? It walks the menu into Free For All.
  - Stuck spectating? It presses Join.
  - Disconnected? It hits Reconnect, and if that doesn't work it restarts Roblox and rejoins.
- **Keeps Roblox fullscreen.** If Roblox ends up in a window, it presses F11 for you.
- **Recovers by itself.** If Roblox closes, crashes or gets stuck mid-run, it reopens Rivals
  and gets back into a match. It also keeps your PC from going to sleep while it runs.
- **Pauses when you tab out.** It only does anything while Rivals is the window in front, so
  you can still use your PC and it won't mess with anything.

## What you need

- Windows 10 or 11
- A **1920×1080** screen. Other sizes aren't supported yet, and the app tells you if yours
  is different. Bigger screens are planned for a future update.
- Windows display scaling at **100%**. If yours is different, change it in Settings →
  System → Display → **Scale** before using the macro. The app pops up a warning if it isn't.
- Roblox Rivals, obviously

## Installing it

1. Go to **[Releases](../../releases/latest)** and download `GlassMacro-v1.1.0.zip`.
   Only get it from here, not from random re-uploads.
2. Right-click the zip → **Extract All**. Don't run it from inside the zip.
3. Open the folder and run **`GlassMacro.exe`**.

Windows will probably warn you the first time. That's normal, and the section
[If Windows or your antivirus warns you](#if-windows-or-your-antivirus-warns-you)
below explains what to do.

Your settings are saved in `%LOCALAPPDATA%\GlassMacro`. If you ever want it gone,
delete the GlassMacro folder and that one, and that's everything.

## Setting it up (like 30 seconds, one time)

The macro just needs to learn where your weapons are. Everything else is already set up.

1. In Rivals, open the **weapon picker**.
2. In GlassMacro, press **I'm on the weapon picker · start setup**. The app walks you
   through it one step at a time, with a little picture of what to hover.
3. Hover over each of these and press **F8**. Don't click, just hover.
   1. the **Random** tile
   2. the **Grenade Launcher** button itself
   3. your **first loadout slot** along the top

Messed up? Press **Esc** to cancel and start again. If an event ever adds or removes
weapons and the grid moves, hit **Redo setup** and do the same three hovers.

## Using it

- Press **Start** at the top of the window, or **F8** while you're in Rivals. **F8** again
  stops it.
- You can start it from anywhere: the hub, mid-match or spectating. It figures out where
  you are.
- The pages are down the left side. Make the window narrow and the sidebar folds into a
  strip of icons (**Ctrl+B** switches it by hand).
- **Home** shows what it's doing right now. The dot pulses while it's working: green while
  it's playing, amber when it's paused or reconnecting. The pill at the top says the same
  thing on every page.
- **Playtime** is the big number. It also shows how many loadouts it's picked, how many
  times it's rejoined or recovered, and your playtime today. Your last run stays on screen
  after you stop.
- **Stats** keeps your lifetime totals: playtime, runs, your longest run, loadouts,
  rejoins and a chart of the last 14 days.
- **Activity** lists what happened in plain English, newest first, and can be filtered to
  just loadouts or problems. **Log** has every detail, which helps with bug reports.
- **It updates itself.** When a new version is out, it asks *"v1.1.1 is available. Do you
  want to update?"* Click **Yes** and it downloads the update, closes, updates and opens
  again. Your weapon setup, settings and stats stay. It never asks in the middle of a run.

The other pages:
- **Weapons**: the setup, **Redo setup**, and a **Live test** of the detection. Leave
  **Match sensitivity** at 0.82 unless picks get skipped.
- **Way back**: how it gets back into Free For All. It's built in, and you can teach it
  your own way if the menu ever changes.
- **Discord**: optional alerts, see below.
- **Settings**: turn off **Keep Roblox fullscreen**, **Reconnect automatically** or
  **Save screenshots** (they're remembered now), and open the data folder, which has the
  log and the pictures it saves.
- **About**: the version, **Check for updates**, and every keyboard shortcut.

## Discord alerts

GlassMacro can post to a Discord channel of yours, so you can check on a long run from
your phone. It's **off** until you set it up, and it only ever posts to the one link you
give it.

1. In Discord, open the channel's settings → **Integrations** → **Webhooks** →
   **New Webhook** → **Copy Webhook URL**.
2. In GlassMacro, open the **Discord** page, press **Paste**, then **Save**.
3. Turn on **Send alerts to Discord** and press **Send test** to check it arrives.

You pick which alerts it sends:
- a run starts or stops
- every full hour of playtime
- it stopped because of an error
- it's stuck: Roblox keeps closing, or it can't get into a match
- it's been paused for 10 minutes or more
- recoveries (reopened, restarted or reconnected) and installed updates, both off at first

Each alert is a short message: what happened (for an error, the error line from the log),
with some of: the run's playtime, loadouts and rejoins, and your lifetime playtime. The
footer has the app version and the run number. If you add your Discord user ID it can **@ you**, but only for errors, being
stuck or a long pause. Nothing else is sent: no screenshots, no PC name, no user name and
no file paths. Apart from **Send test**, alerts only go out during a run you started.

Anyone who has your webhook link can post in that channel, so keep it to yourself. The app
shows it masked once it's saved. It's stored in `settings.json` in your data folder, so
don't share that file.

## If Windows or your antivirus warns you

GlassMacro isn't code-signed, because that costs money every year. Windows is careful with
any new app that hasn't been signed. It doesn't mean something's wrong with it.

**"Windows protected your PC"**: click **More info**, check it says `GlassMacro.exe`,
then click **Run anyway**. That only allows this one file.

**Your browser says the file "isn't commonly downloaded"**: choose **Keep**. In Edge
it's **…** → **Keep** → **Show more** → **Keep anyway**.

**"Smart App Control blocked this app"**: this one has no Run anyway button. The only
way around it is turning Smart App Control off, which makes your PC less protected. That's
totally your call, and it's fine to skip GlassMacro instead.

**Your antivirus quarantines it**: don't restore it. Open an [issue](../../issues) with
the exact detection name and I'll get it reported as a false positive.

**Please never** turn off Defender or real-time protection, or add exclusions for whole
folders like Downloads, just to get a program running. GlassMacro doesn't need that, and
nothing you download ever should.

## Good to know

- **Use it at your own risk.** Macros can go against Roblox's or the game's rules, and your
  account is your responsibility.
- **I'm not affiliated with Roblox or Rivals, and I'm not partnered or collaborating with any
  other macro developers.** GlassMacro is a solo project, made by me.
- **What it actually does on your PC:**
  - It looks at your screen to see what's showing, and only uses your keyboard and mouse
    inside Rivals.
  - It listens for F8 to start and stop, plus Esc during setup. It doesn't record anything
    else you type.
  - The only program it ever closes or reopens is Roblox.
  - It only ever contacts two places:
    - **GitHub**, to check whether there's a newer version and, if you click Yes, to
      download it. It only installs updates from this repo's releases, and only if the
      download's fingerprint matches what GitHub lists. Nothing about you or your game is
      sent, and you can turn update checks off in About → **Check for updates**.
    - **Discord**, but only if you turn on [Discord alerts](#discord-alerts) and paste your
      own webhook link. It's off until you do, and it only posts to that one link.
  - The pictures it saves stay in your data folder. They're never uploaded anywhere,
    including to Discord.

## Found a bug?

Open an [issue](../../issues) and tell me what happened. Attaching your `log.txt`
(Settings → **Open data folder**) helps a ton, and so do the pictures in the `events`
folder, since they show exactly what the macro saw.

## Building it yourself

If you'd rather build it from the code, you need Python 3.14 on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\pyinstaller --noconfirm --clean --onedir --windowed --noupx --name GlassMacro `
  --icon glassmacro.ico --add-data "glassmacro.ico;." --version-file version_info.txt glassmacro.py
```

The app ends up in `dist\GlassMacro\`. `python make_icon.py` redraws the icon.
`python test_detectors.py` checks the screen detection against real screenshots, which you
need to provide yourself because they aren't in the repo.

## License

[MIT](LICENSE). Do what you want with it, just keep the license file.

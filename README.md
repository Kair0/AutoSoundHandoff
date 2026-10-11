# AutoSound Handoff

**Automatic audio priority for Windows.** Pick the music or media player you want running in the background. AutoSound Handoff watches other applications' audio output, pauses your preferred player when another app makes enough sound, and resumes it once the competing audio becomes quiet.

Built with Python, [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter), [pycaw](https://github.com/AndreMiras/pycaw), and Windows media-session APIs.

> **Status:** Windows 10/11 prototype. Audio handoff has been tested with Spotify and browser video in development, but compatibility varies across players, devices, and Windows configurations.

## Features

- **Preferred player:** Select a compatible Windows media session (e.g., Spotify) as the background player.
- **Automatic pause and resume:** Detect audio from other processes and hand control back when they become quiet.
- **Adjustable sensitivity:** Set a peak-audio interruption threshold, pause confirmation delay, and quiet-time resume delay.
- **Live monitoring dashboard:** See the loudest competing application, its audio level, and the current handoff status.
- **Searchable exclusions:** Ignore selected applications so they don't interrupt your music; show audio apps by default or expand to all running processes.
- **Manual-pause protection:** By default, the app resumes only playback it previously paused. An optional *Play when quiet* setting can start playback even when you manually paused it.
- **System tray and saved settings:** Keep the app out of the way and preserve preferences between launches.
- **Optional Windows sign-in launch:** Supported when running from Python; see the packaging caveat below.

## Requirements

- Windows 10 or 11
- Python 3.10+ and a compatible build of `winsdk` (Python 3.11 or 3.12 is a reasonable fallback if installation fails)
- A media player that exposes a controllable **Windows System Media Transport Controls** session

## Run from source

Clone this repository, open PowerShell in its directory, and run:

```powershell
git clone https://github.com/YOUR-USERNAME/AutoSoundHandoff.git
cd AutoSoundHandoff
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Replace `YOUR-USERNAME` with the actual repository owner, or download the source and run the commands from its folder.

If your virtual environment already exists, skip the `py -3 -m venv .venv` command.

## Quick start

1. Open your preferred music or media player and play something so Windows creates a media session.
2. On the **Dashboard**, choose that player in **Preferred player**.
3. Select its audio process (for example, `spotify.exe`) under **Audio process to ignore**. The controller must ignore its own preferred player when checking for competing audio.
4. Select **Start monitoring**.
5. Play a video or sound in a different application; the preferred player should pause after the configured delay. Once the other audio stays quiet for the resume delay, it should resume.
6. Use **Settings & exclusions** to adjust timing, threshold, and applications to ignore.

### Example

| What happens | Expected behavior |
| --- | --- |
| Only Spotify is playing | Spotify continues |
| Chrome or Zen Browser starts making sound above threshold | Spotify pauses |
| Browser video reaches a silent section | Spotify resumes after the quiet-time delay |
| Another non-excluded app is still making significant sound | Spotify stays paused |

The audio threshold is a **percentage of digital peak amplitude**, not a percentage of perceived loudness. A higher threshold means quieter sounds are less likely to interrupt the preferred player.

## Build a Windows `.exe`

Install PyInstaller inside your virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
```

**Single-file build** (no console window):

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name "AutoSoundHandoff" --collect-all customtkinter --collect-submodules winsdk main.py
```

Output: `dist\AutoSoundHandoff.exe`.

**Folder build** (recommended for debugging DLL/import problems):

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --windowed --name "AutoSoundHandoff" --collect-all customtkinter --collect-submodules winsdk main.py
```

Output: `dist\AutoSoundHandoff\AutoSoundHandoff.exe` (keep the adjacent files together).

Build on Windows; PyInstaller does not cross-compile Windows binaries from macOS or Linux. Test the packaged app on a Windows machine before publishing a release. If necessary, remove `--windowed` temporarily to expose console errors.

> **Packaged startup caveat:** The current `set_autostart()` implementation constructs a command using a Python interpreter **and** the `.py` source path. When frozen into an executable, it needs a `sys.frozen` check and a command pointing directly at `sys.executable`; otherwise **Launch at Windows sign-in** may register an invalid command. Until that is fixed, don't rely on this option in the `.exe` build.

## Settings and logs

The application stores settings and diagnostic logs in:

```text
%APPDATA%\AutoSoundHandoff\settings.json
%APPDATA%\AutoSoundHandoff\autosoundhandoff.log
```

To view recent logs in PowerShell:

```powershell
Get-Content "$env:APPDATA\AutoSoundHandoff\autosoundhandoff.log" -Tail 40
```

## Known limitations

- **Media controls:** Not every application exposes a Windows media session or supports targeted Play and Pause commands.
- **Browser tabs:** Audio detection is based on process names, not individual tabs. Selecting a browser as the preferred player generally excludes audio from *all* of that browser's processes/tabs.
- **Output device:** Audio enumeration uses the default Windows playback endpoint. Apps routed through a different output device may not be detected.
- **Manual playback:** When *Play when quiet* is enabled, the application may start playback that you manually paused. Keep it disabled if you want manual-pause protection.
- **Monitoring stopped:** Stopping monitoring does not automatically resume a player that the app had paused.
- **Session availability:** A preferred player may need to be opened and playing briefly before its media session appears in the dropdown.
- **Packaged sign-in launch:** Requires the executable-aware startup fix described above.

## Troubleshooting

**Preferred player does not appear:** Start playback in the player and click **Refresh**. Verify that it provides a Windows media session.

**A browser does not interrupt playback:** Check **Settings & exclusions** to confirm that the browser is not excluded. Reduce the threshold, verify the default Windows playback device, and inspect the live activity meter and log.

**App starts but doesn't control playback:** The selected media session might not support targeted playback commands. Try another supported player and check the log.

**Packaged `.exe` closes or fails to launch:** Try the `--onedir` build, remove `--windowed` while debugging, and inspect the console error and log.

## Project structure

```text
AutoSoundHandoff/
├── main.py            # GUI, audio monitoring and handoff logic
├── requirements.txt   # Python dependencies
├── LICENSE            # MIT License
└── README.md          # Project documentation
```

## Contributing

Bug reports and pull requests are welcome. Please include your Windows version, preferred media player, output device setup, steps to reproduce, and relevant log messages (after reviewing them for private information).

## License

This project is licensed under the **MIT License**. You are free to use, modify, distribute, and sublicense the code, provided that the copyright and license notice are included. See the [LICENSE](LICENSE) file for the full terms.

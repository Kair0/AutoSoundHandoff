import asyncio
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import comtypes
import customtkinter as ctk
import pystray
import psutil
from PIL import Image, ImageDraw
from pycaw.pycaw import AudioUtilities
from pycaw.api.endpointvolume import IAudioMeterInformation

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
except ImportError as exc:
    raise SystemExit(
        "Windows media API missing. Run: python -m pip install -r requirements.txt\n"
        f"Details: {exc}"
    ) from exc

BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / "AutoSoundHandoff"
BASE.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE / "settings.json"
logging.basicConfig(
    filename=str(BASE / "autosoundhandoff.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

DEFAULTS = {
    "preferred_id": "",
    "preferred_process": "",
    "excluded_processes": [],
    "threshold": 0.025,
    "resume_ratio": 0.7,
    "pause_delay": 0.35,
    "resume_delay": 1.5,
    "start_when_idle": False,
    "launch_at_login": False,
    "minimize_to_tray": True,
}


def load_config():
    config = DEFAULTS.copy()
    try:
        content = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(content, dict):
            for key in DEFAULTS:
                if key in content and isinstance(content[key], type(DEFAULTS[key])):
                    config[key] = content[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logging.exception("Could not read settings")
    config["threshold"] = max(.001, min(.20, config["threshold"]))
    config["pause_delay"] = max(.1, min(5., config["pause_delay"]))
    config["resume_delay"] = max(.3, min(15., config["resume_delay"]))
    return config


def save_config(config):
    temp = CONFIG_PATH.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        temp.replace(CONFIG_PATH)
    except OSError:
        logging.exception("Could not save settings")


def get_peaks():
    """Read audio sessions on Windows' DEFAULT render output endpoint.

    Keep zero-level sessions visible for diagnosis. Some applications expose a
    ProcessId even when pycaw cannot resolve session.Process.
    """
    result = {}
    errors = []
    count = 0
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception as exc:
        raise RuntimeError(f"Unable to read Windows output audio sessions: {exc}") from exc
    for session in sessions:
        count += 1
        try:
            process = session.Process
            name = process.name().lower() if process else ""
            if not name:
                pid = getattr(session, "ProcessId", 0)
                if pid:
                    name = psutil.Process(pid).name().lower()
            if not name or name in ("audiodg.exe", "system"):
                continue
            # pycaw AudioSession does NOT provide .AudioMeterInformation.
            # Obtain the per-session peak meter via COM QueryInterface.
            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            peak = float(meter.GetPeakValue())
            result[name] = max(result.get(name, 0.), max(0., min(1., peak)))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    if errors:
        logging.warning("Audio sessions skipped (%s): %s", len(errors), errors[:5])
    return result, count, len(errors)


def list_running_processes():
    """Discover processes even when they have no current audio session."""
    names = set()
    for process in psutil.process_iter(attrs=["name"]):
        try:
            if process.info["name"]:
                names.add(process.info["name"].lower())
        except (psutil.Error, OSError):
            continue
    return sorted(names)


def infer_preferred_process(media_id):
    """Conservative inference for the Spotify Windows app only."""
    if "spotify" in media_id.lower():
        return "spotify.exe"
    return ""


def playback_is_playing(session):
    return session.get_playback_info().playback_status == PlaybackStatus.PLAYING


class Engine:
    """All COM and Windows media calls stay on the same worker thread."""

    def __init__(self, updates):
        self.updates = updates
        self.actions = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.running = False
        self.config = load_config()
        self.claimed_pause = False
        self.pause_command_at = None
        self.last_preferred = None
        self.last_other = False
        self.loud_since = None
        self.quiet_since = None
        self.suppress_until_quiet = False
        self.last_scan = 0.
        self.last_status = ""

    def start(self):
        self.thread.start()

    def send(self, action, value=None):
        self.actions.put((action, value))

    def close(self):
        self.stop_event.set()

    def notify(self, kind, payload):
        self.updates.put((kind, payload))

    def reset_handoff(self):
        # Intentionally never resumes automatically when monitoring is stopped.
        self.claimed_pause = False
        self.pause_command_at = None
        self.last_other = False
        self.loud_since = None
        self.quiet_since = None
        self.suppress_until_quiet = False

    def set_status(self, status):
        if status != self.last_status:
            self.last_status = status
            self.notify("status", status)

    def _thread_main(self):
        comtypes.CoInitialize()
        try:
            asyncio.run(self._run())
        except Exception:
            logging.exception("Engine failed")
            self.notify("error", "Audio service stopped unexpectedly; see log.")
        finally:
            comtypes.CoUninitialize()

    async def _run(self):
        try:
            manager = await MediaManager.request_async()
        except Exception as exc:
            logging.exception("Windows media-session service unavailable")
            self.notify("error", f"Windows media-session API: {exc}")
            return

        self.set_status("Idle — choose a preferred player")
        while not self.stop_event.is_set():
            try:
                await self._handle_actions()
                now = time.monotonic()
                peaks, session_count, skipped = get_peaks()
                sessions = manager.get_sessions()
                if now - self.last_scan >= 2.0:
                    ids = sorted({s.source_app_user_model_id for s in sessions})
                    available = sorted(set(peaks) | set(list_running_processes()))
                    self.notify("discovery", {"media": ids, "processes": available, "audio": sorted(peaks)})
                    self.last_scan = now
                await self._tick(now, peaks, sessions, session_count, skipped)
            except Exception as exc:
                logging.exception("Monitor iteration failed")
                self.notify("warning", str(exc))
            await asyncio.sleep(.15)

    async def _handle_actions(self):
        while True:
            try:
                action, value = self.actions.get_nowait()
            except queue.Empty:
                return
            if action == "config":
                old_id = self.config.get("preferred_id")
                old_process = self.config.get("preferred_process")
                self.config = value.copy()
                if old_id != value.get("preferred_id") or old_process != value.get("preferred_process"):
                    self.reset_handoff()
            elif action == "refresh":
                self.last_scan = 0.
            elif action == "start":
                self.running = True
                self.reset_handoff()
            elif action == "stop":
                self.running = False
                self.reset_handoff()
                self.set_status("Stopped — no automatic playback changes")

    async def _tick(self, now, peaks, sessions, session_count, skipped):
        config = self.config
        preferred_id = config["preferred_id"]
        excluded = {x.lower() for x in config["excluded_processes"]}
        preferred_process = (config["preferred_process"] or infer_preferred_process(preferred_id)).lower()
        if preferred_process:
            excluded.add(preferred_process)
        # Never count the app's own process as competing audio.
        excluded.update({"python.exe", "pythonw.exe"})
        others = {name: peak for name, peak in peaks.items() if name not in excluded}
        winner = max(others, key=others.get) if others else ""
        max_peak = others.get(winner, 0.)
        self.notify("levels", {"peaks": peaks, "winner": winner, "peak": max_peak,
                               "session_count": session_count, "skipped": skipped,
                               "excluded": sorted(excluded)})

        if not self.running:
            return
        if session_count == 0:
            self.set_status("No sessions on default output device — check Windows Sound > Volume mixer")
            return
        if not preferred_id:
            self.set_status("Select a preferred media session")
            return
        if not preferred_process:
            self.set_status("Select the preferred player's process to prevent self-interruption")
            return
        matches = [s for s in sessions if s.source_app_user_model_id == preferred_id]
        if not matches:
            # Do not act on another session when the chosen session disappears.
            self.reset_handoff()
            self.set_status("Preferred media session unavailable")
            return
        if len(matches) != 1:
            self.reset_handoff()
            self.set_status("Ambiguous player: multiple media sessions share its ID")
            return
        preferred = matches[0]
        playing = playback_is_playing(preferred)
        activation = config["threshold"]
        release = activation * config["resume_ratio"]
        loud = max_peak >= (release if self.last_other else activation)
        self.last_other = loud

        if loud:
            self.quiet_since = None
            if self.loud_since is None:
                self.loud_since = now
            if self.claimed_pause:
                # If someone presses Play while we're holding a pause, respect it.
                if playing and self.pause_command_at is not None and now - self.pause_command_at > 1.0:
                    self.claimed_pause = False
                    self.suppress_until_quiet = True
                    self.set_status("Manual playback detected — automation yielding")
                else:
                    self.set_status(f"Paused for {winner} ({max_peak*100:.1f}%)")
                return
            if self.suppress_until_quiet:
                self.set_status("Manual override — waiting for quiet")
                return
            if now - self.loud_since < config["pause_delay"]:
                self.set_status(f"Audio detected: {winner} ({max_peak*100:.1f}%)")
                return
            if playing:
                if await preferred.try_pause_async():
                    self.claimed_pause = True
                    self.pause_command_at = now
                    self.set_status(f"Paused preferred player for {winner}")
                    logging.info("Paused %s due to %s", preferred_id, winner)
                else:
                    self.set_status("Preferred player rejected Pause")
            else:
                self.set_status("Preferred player already paused (manual pause protected)")
            return

        self.loud_since = None
        self.suppress_until_quiet = False
        if self.quiet_since is None:
            self.quiet_since = now
        if now - self.quiet_since < config["resume_delay"]:
            self.set_status("Others quiet — waiting to resume")
            return
        if self.claimed_pause:
            if playing:
                self.claimed_pause = False
                self.set_status("Already playing — no resume needed")
            elif await preferred.try_play_async():
                self.claimed_pause = False
                self.set_status("Resumed preferred player")
                logging.info("Resumed %s", preferred_id)
            else:
                self.set_status("Preferred player rejected Play")
        elif config["start_when_idle"]:
            # Explicit opt-in: may restart playback the user paused manually.
            if not playing:
                if await preferred.try_play_async():
                    self.set_status("Started preferred player (auto-start enabled)")
                else:
                    self.set_status("Preferred player rejected Play")
            else:
                self.set_status(f"Preferred player playing — other apps below {activation*100:.1f}% threshold")
        else:
            self.set_status("Quiet — respecting current player state")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoSound Handoff")
        self.geometry("740x750")
        self.minsize(590, 570)
        ctk.set_appearance_mode("System")
        self.config_data = load_config()
        self.messages = queue.Queue()
        self.engine = Engine(self.messages)
        self.media_ids = []
        self.process_names = []
        self.closing = False
        self.tray = None
        self.widgets_exclusions = {}
        self.audio_names = []
        self.exclusion_signature = None
        self.active_page = "dashboard"
        self.exclusion_query = ""
        self.build_ui()
        self.engine.start()
        self.after(100, self.process_messages)
        self.protocol("WM_DELETE_WINDOW", self.hide_or_quit)
        self.create_tray()

    def build_ui(self):
        # A fixed header, tab-like navigation, and fixed bottom action area.
        self.configure(fg_color=("#F2F5F8", "#10141B"))
        self.CARD = ("#FFFFFF", "#1B222C")
        self.MUTED = ("#627084", "#99A8BA")
        self.ACCENT = "#3988F6"

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 9))
        ctk.CTkLabel(header, text="◉", text_color=self.ACCENT,
                     font=ctk.CTkFont(size=28, weight="bold")).pack(side="left", padx=(0, 9))
        title = ctk.CTkFrame(header, fg_color="transparent")
        title.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(title, text="AutoSound", anchor="w",
                     font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title, text="Smart audio handoff for Windows", anchor="w",
                     text_color=self.MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w")
        ctk.CTkButton(header, text="↻  Refresh", width=100, height=34,
                      fg_color=self.CARD, text_color=("#25354D", "#E1E9F5"),
                      hover_color=("#DEE8F7", "#303B4A"), command=self.on_refresh).pack(side="right")

        nav = ctk.CTkFrame(self, fg_color=self.CARD, corner_radius=14)
        nav.pack(fill="x", padx=24, pady=(4, 12))
        self.dashboard_tab = ctk.CTkButton(nav, text="Dashboard", height=38,
            command=lambda: self.switch_page("dashboard"), corner_radius=10)
        self.dashboard_tab.pack(side="left", expand=True, fill="x", padx=(5, 2), pady=5)
        self.settings_tab = ctk.CTkButton(nav, text="Settings & exclusions", height=38,
            command=lambda: self.switch_page("settings"), corner_radius=10)
        self.settings_tab.pack(side="left", expand=True, fill="x", padx=(2, 5), pady=5)

        self.pages = ctk.CTkFrame(self, fg_color="transparent")
        self.pages.pack(fill="both", expand=True, padx=24)
        self.dashboard_page = ctk.CTkScrollableFrame(self.pages, fg_color="transparent", corner_radius=0)
        self.settings_page = ctk.CTkScrollableFrame(self.pages, fg_color="transparent", corner_radius=0)

        player_card = self.card(self.dashboard_page, "PREFERRED PLAYER", "Choose what should play in the background")
        self.media_combo = ctk.CTkComboBox(player_card, values=["Searching for media sessions..."],
            state="readonly", height=37, command=self.on_media_change)
        self.media_combo.pack(fill="x", padx=17, pady=(3, 16))
        ctk.CTkLabel(player_card, text="Audio process to ignore", text_color=self.MUTED,
                     anchor="w", font=ctk.CTkFont(size=12)).pack(fill="x", padx=17)
        self.process_combo = ctk.CTkComboBox(player_card, values=["Searching..."],
            state="readonly", height=35, command=self.on_process_change)
        self.process_combo.pack(fill="x", padx=17, pady=(4, 17))

        activity_card = self.card(self.dashboard_page, "LIVE AUDIO", "Loudest competing application")
        self.activity_label = ctk.CTkLabel(activity_card, text="No competing audio", anchor="w",
             font=ctk.CTkFont(size=16, weight="bold"))
        self.activity_label.pack(fill="x", padx=17, pady=(6, 8))
        self.meter = ctk.CTkProgressBar(activity_card, height=11, progress_color=self.ACCENT)
        self.meter.pack(fill="x", padx=17, pady=(0, 7))
        self.meter.set(0)
        self.details_label = ctk.CTkLabel(activity_card, text="Waiting for audio data…",
             text_color=self.MUTED, anchor="w", justify="left", wraplength=550,
             font=ctk.CTkFont(size=12))
        self.details_label.pack(fill="x", padx=17, pady=(3, 17))

        self.status_card = ctk.CTkFrame(self.dashboard_page, fg_color=self.CARD, corner_radius=14)
        self.status_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(self.status_card, text="AUTOMATION", text_color=self.MUTED,
            font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=17, pady=(15, 4))
        self.state_label = ctk.CTkLabel(self.status_card, text="Ready to monitor",
            anchor="w", font=ctk.CTkFont(size=14, weight="bold"), wraplength=530)
        self.state_label.pack(fill="x", padx=17, pady=(0, 16))

        timing = self.card(self.settings_page, "HANDOFF SENSITIVITY", "Tune how quickly apps take priority")
        self.threshold_label = ctk.CTkLabel(timing, text="Interrupt threshold", anchor="w")
        self.threshold_label.pack(fill="x", padx=17, pady=(4, 2))
        self.threshold_slider = ctk.CTkSlider(timing, from_=0.1, to=20.0,
            number_of_steps=199, command=self.on_threshold)
        self.threshold_slider.set(self.config_data["threshold"] * 100)
        self.threshold_slider.pack(fill="x", padx=18, pady=(0, 13))
        self.on_threshold(self.config_data["threshold"] * 100, save=False)
        self.pause_label = ctk.CTkLabel(timing, text="Pause delay", anchor="w")
        self.pause_label.pack(fill="x", padx=17, pady=(0, 2))
        self.pause_slider = ctk.CTkSlider(timing, from_=0.1, to=3.0,
            number_of_steps=29, command=self.on_pause_delay)
        self.pause_slider.set(self.config_data["pause_delay"])
        self.pause_slider.pack(fill="x", padx=18, pady=(0, 13))
        self.on_pause_delay(self.config_data["pause_delay"], save=False)
        self.resume_label = ctk.CTkLabel(timing, text="Resume delay", anchor="w")
        self.resume_label.pack(fill="x", padx=17, pady=(0, 2))
        self.resume_slider = ctk.CTkSlider(timing, from_=0.5, to=10.,
            number_of_steps=95, command=self.on_resume_delay)
        self.resume_slider.set(self.config_data["resume_delay"])
        self.resume_slider.pack(fill="x", padx=18, pady=(0, 17))
        self.on_resume_delay(self.config_data["resume_delay"], save=False)

        options = self.card(self.settings_page, "BEHAVIOR", "Playback and Windows preferences")
        self.auto_start = ctk.CTkCheckBox(options,
            text="Play when quiet (may override manual pause)", command=self.on_options)
        self.auto_start.pack(anchor="w", padx=17, pady=(6, 12))
        if self.config_data["start_when_idle"]: self.auto_start.select()
        self.minimize_opt = ctk.CTkCheckBox(options, text="Close to system tray", command=self.on_options)
        self.minimize_opt.pack(anchor="w", padx=17, pady=(0, 12))
        if self.config_data["minimize_to_tray"]: self.minimize_opt.select()
        self.startup_opt = ctk.CTkCheckBox(options, text="Launch at Windows sign-in", command=self.on_options)
        self.startup_opt.pack(anchor="w", padx=17, pady=(0, 17))
        if self.config_data["launch_at_login"]: self.startup_opt.select()

        exclusions = self.card(self.settings_page, "EXCLUDED APPLICATIONS",
                              "Checked apps won't interrupt your preferred player")
        self.exclusion_search = ctk.CTkEntry(exclusions, height=34,
                                             placeholder_text="Search application names…")
        self.exclusion_search.pack(fill="x", padx=17, pady=(4, 8))
        self.exclusion_search.bind("<KeyRelease>", self.on_exclusion_search)
        self.show_all = ctk.CTkCheckBox(exclusions, text="Show all running processes",
              font=ctk.CTkFont(size=12), command=self.redraw_exclusions)
        self.show_all.pack(anchor="w", padx=17, pady=(0, 6))
        self.exclusion_count = ctk.CTkLabel(exclusions, text="Looking for audio applications…",
                                             anchor="w", text_color=self.MUTED,
                                             font=ctk.CTkFont(size=12))
        self.exclusion_count.pack(fill="x", padx=17, pady=(0, 5))
        self.exclusions_area = ctk.CTkScrollableFrame(exclusions, height=175, fg_color="transparent")
        self.exclusions_area.pack(fill="x", padx=12, pady=(0, 13))

        footer = ctk.CTkFrame(self, fg_color=self.CARD, corner_radius=0)
        footer.pack(side="bottom", fill="x")
        self.status_label = ctk.CTkLabel(footer, text="Starting audio service…", anchor="w",
                                        text_color=self.MUTED, wraplength=580)
        self.status_label.pack(fill="x", padx=24, pady=(9, 4))
        self.toggle_button = ctk.CTkButton(footer, text="▶  Start monitoring",
            height=42, corner_radius=10, command=self.on_toggle)
        self.toggle_button.pack(fill="x", padx=24, pady=(0, 13))
        self.switch_page("dashboard")

    def card(self, parent, heading, subtitle):
        frame = ctk.CTkFrame(parent, fg_color=self.CARD, corner_radius=14)
        frame.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(frame, text=heading, font=ctk.CTkFont(size=12, weight="bold"),
                     anchor="w").pack(fill="x", padx=17, pady=(15, 1))
        ctk.CTkLabel(frame, text=subtitle, text_color=self.MUTED,
                     font=ctk.CTkFont(size=12), anchor="w").pack(fill="x", padx=17, pady=(0, 8))
        return frame

    def switch_page(self, page):
        self.active_page = page
        self.dashboard_page.pack_forget()
        self.settings_page.pack_forget()
        selected = self.dashboard_page if page == "dashboard" else self.settings_page
        selected.pack(fill="both", expand=True)
        for tab, tab_name in ((self.dashboard_tab, "dashboard"), (self.settings_tab, "settings")):
            active = tab_name == page
            tab.configure(fg_color=self.ACCENT if active else "transparent",
                          text_color="#FFFFFF" if active else self.MUTED,
                          hover_color="#2470D9" if active else ("#EEF2F7", "#303B4A"))
        if page == "settings":
            self.redraw_exclusions()

    def on_exclusion_search(self, event=None):
        self.redraw_exclusions()

    def persist(self):
        save_config(self.config_data)
        self.engine.send("config", self.config_data.copy())

    def on_media_change(self, selected):
        if selected in self.media_ids:
            self.config_data["preferred_id"] = selected
            # Infer Spotify's audio process even if it isn't currently audible.
            inferred = infer_preferred_process(selected)
            if inferred:
                self.config_data["preferred_process"] = inferred
                self.process_combo.set(inferred)
            elif not self.config_data["preferred_process"]:
                candidates = [p for p in self.process_names if
                              p.removesuffix(".exe") in selected.lower()]
                if len(candidates) == 1:
                    self.config_data["preferred_process"] = candidates[0]
                    self.process_combo.set(candidates[0])
            self.persist()

    def on_process_change(self, selected):
        if selected in self.process_names:
            self.config_data["preferred_process"] = selected
            self.persist()
            self.redraw_exclusions()

    def on_threshold(self, value, save=True):
        self.config_data["threshold"] = round(float(value) / 100., 4)
        self.threshold_label.configure(text=f"Interrupt threshold: {value:.1f}% peak")
        if save: self.persist()

    def on_pause_delay(self, value, save=True):
        self.config_data["pause_delay"] = round(float(value), 2)
        self.pause_label.configure(text=f"Pause after {value:.1f} s sustained audio")
        if save: self.persist()

    def on_resume_delay(self, value, save=True):
        self.config_data["resume_delay"] = round(float(value), 2)
        self.resume_label.configure(text=f"Resume after {value:.1f} s sustained quiet")
        if save: self.persist()

    def on_options(self):
        self.config_data["start_when_idle"] = bool(self.auto_start.get())
        self.config_data["minimize_to_tray"] = bool(self.minimize_opt.get())
        requested = bool(self.startup_opt.get())
        if requested != self.config_data["launch_at_login"]:
            if self.set_autostart(requested):
                self.config_data["launch_at_login"] = requested
            else:
                if requested: self.startup_opt.deselect()
                else: self.startup_opt.select()
        self.persist()

    def redraw_exclusions(self):
        if self.active_page != "settings":
            return
        excluded = set(self.config_data["excluded_processes"])
        names = self.process_names if self.show_all.get() else self.audio_names
        # Always show saved exclusions, even if an app is no longer running.
        names = set(names) | excluded
        names.discard(self.config_data["preferred_process"])
        query = self.exclusion_search.get().strip().lower()
        visible = sorted((name for name in names if query in name),
                         key=lambda name: (name not in excluded, name))
        signature = (tuple(visible), tuple(sorted(excluded)))
        if signature == self.exclusion_signature:
            return  # Avoid destroying checkboxes on every process refresh.
        self.exclusion_signature = signature
        for widget in self.exclusions_area.winfo_children():
            widget.destroy()
        self.exclusion_count.configure(text=f"{len(excluded)} excluded  ·  {len(visible)} shown")
        if not visible:
            ctk.CTkLabel(self.exclusions_area,
                text="No matches. Enable 'Show all' for apps without audio sessions.",
                text_color=self.MUTED, wraplength=420).pack(pady=14)
            return
        for process in visible:
            box = ctk.CTkCheckBox(self.exclusions_area, text=process,
                command=lambda name=process: self.toggle_exclusion(name))
            box.pack(anchor="w", padx=8, pady=5)
            if process in excluded:
                box.select()

    def toggle_exclusion(self, process):
        values = set(self.config_data["excluded_processes"])
        if process in values: values.remove(process)
        else: values.add(process)
        self.config_data["excluded_processes"] = sorted(values)
        self.persist()

    def on_refresh(self):
        self.engine.send("refresh")
        self.status_label.configure(text="Refreshing available applications…")

    def on_toggle(self):
        if self.engine.running:
            self.engine.send("stop")
            self.engine.running = False
            self.toggle_button.configure(text="▶  Start monitoring")
        else:
            if not self.config_data["preferred_id"]:
                self.status_label.configure(text="Choose a preferred media player first")
                return
            if not (self.config_data["preferred_process"] or infer_preferred_process(self.config_data["preferred_id"])):
                self.status_label.configure(text="Choose its audio process first (required to prevent self-interruption)")
                return
            self.engine.send("start")
            self.engine.running = True
            self.toggle_button.configure(text="■  Stop monitoring")

    def process_messages(self):
        if self.closing:
            return
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "tray_show":
                    self.deiconify()
                    self.lift()
                elif kind == "tray_quit":
                    self.quit_app()
                    return
                elif kind == "discovery":
                    media, processes = value["media"], value["processes"]
                    audio = value.get("audio", [])
                    if audio != self.audio_names:
                        self.audio_names = audio
                        self.redraw_exclusions()
                    if media != self.media_ids:
                        self.media_ids = media
                        self.media_combo.configure(values=media or ["No media sessions"])
                        chosen = self.config_data["preferred_id"]
                        self.media_combo.set(chosen if chosen in media else "Select a media player...")
                        if chosen in media and not self.config_data["preferred_process"]:
                            inferred = infer_preferred_process(chosen)
                            if inferred:
                                self.config_data["preferred_process"] = inferred
                                self.process_combo.set(inferred)
                                self.persist()
                    if processes != self.process_names:
                        self.process_names = processes
                        self.process_combo.configure(values=processes or ["No audio processes"])
                        chosen = self.config_data["preferred_process"]
                        self.process_combo.set(chosen if chosen else "Select its process...")
                        self.redraw_exclusions()
                elif kind == "levels":
                    peak = value["peak"]
                    self.meter.set(peak)
                    winner = value["winner"] or "None"
                    self.activity_label.configure(text=f"Loudest other app: {winner}  |  {peak*100:.2f}% peak")
                    seen = sorted(value["peaks"].items(), key=lambda x: x[1], reverse=True)
                    lines = [f"Default output sessions: {value['session_count']}   |   unreadable: {value['skipped']}"]
                    if seen:
                        lines.append("Detected: " + ", ".join(f"{name} {level*100:.2f}%" for name, level in seen[:8]))
                    else:
                        lines.append("No named audio processes detected on the default output device")
                    if value["skipped"]:
                        lines.append("Meter read failures: check autosoundhandoff.log")
                    lines.append("Ignored: " + (", ".join(value["excluded"][:8]) or "none"))
                    self.details_label.configure(text="\n".join(lines))
                elif kind == "status":
                    self.status_label.configure(text=value)
                    self.state_label.configure(text=value)
                elif kind in ("warning", "error"):
                    self.status_label.configure(text=f"{kind.title()}: {value}")
                    if kind == "error":
                        self.engine.running = False
                        self.toggle_button.configure(text="▶  Start monitoring", state="disabled")
        except queue.Empty:
            pass
        self.after(100, self.process_messages)

    def set_autostart(self, enabled):
        """Register a per-user startup command for either Python or a frozen EXE."""
        try:
            import winreg

            registry_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            value_name = "AutoSoundHandoff"

            if enabled:
                if getattr(sys, "frozen", False):
                    # PyInstaller: sys.executable is the actual packaged EXE.
                    arguments = [str(Path(sys.executable).resolve()), "--minimized"]
                else:
                    # Source execution: use pythonw.exe to avoid a console at login.
                    interpreter = Path(sys.executable).resolve()
                    pythonw = interpreter.with_name("pythonw.exe")
                    if pythonw.is_file():
                        interpreter = pythonw
                    arguments = [
                        str(interpreter),
                        str(Path(__file__).resolve()),
                        "--minimized",
                    ]

                command = subprocess.list2cmdline(arguments)
                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER,
                    registry_path,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
                logging.info("Windows sign-in startup enabled: %s", command)
            else:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    registry_path,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    try:
                        winreg.DeleteValue(key, value_name)
                    except FileNotFoundError:
                        pass
                logging.info("Windows sign-in startup disabled")
            return True
        except FileNotFoundError:
            # Deleting a Run value is successful even if no Run key exists.
            if not enabled:
                return True
            logging.exception("Windows startup registry key unavailable")
            return False
        except Exception:
            logging.exception("Could not configure Windows sign-in startup")
            return False

    def create_tray(self):
        img = Image.new("RGB", (64, 64), "#10293e")
        draw = ImageDraw.Draw(img)
        draw.ellipse((10, 10, 54, 54), outline="#55cb98", width=5)
        draw.polygon([(28, 20), (28, 44), (46, 32)], fill="#55cb98")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda icon, item: self.messages.put(("tray_show", None))),
            pystray.MenuItem("Quit", lambda icon, item: self.messages.put(("tray_quit", None))),
        )
        self.tray = pystray.Icon("AutoSoundHandoff", img, "AutoSound Handoff", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def hide_or_quit(self):
        if self.config_data["minimize_to_tray"]:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        if self.closing: return
        self.closing = True
        self.engine.close()
        if self.tray:
            self.tray.stop()
        self.destroy()


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("AutoSound Handoff requires Windows 10/11.")
    application = App()
    if "--minimized" in sys.argv and application.config_data["minimize_to_tray"]:
        application.withdraw()
    application.mainloop()

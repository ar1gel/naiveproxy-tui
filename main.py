#!/usr/bin/env python3
"""
naiveproxy-tui — Terminal UI for NaïveProxy management & VPS deployment.
"""

import sys
import os
import json
import subprocess
import signal
import threading
import time
import queue
from pathlib import Path
from typing import Optional

try:
    import curses
    import curses.textpad
    import curses.ascii
except ImportError:
    print("Error: 'curses' module required. Run on a Unix terminal.")
    sys.exit(1)

# ── Paths (always relative to this script's directory) ─────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = _SCRIPT_DIR / "config.json"
NAIVE_BIN = _SCRIPT_DIR / "naive"
NAIVE_DIR = _SCRIPT_DIR
VPS_CONFIG_FILE = Path.home() / ".config" / "naiveproxy-tui" / "vps.json"
LOG_FILE = _SCRIPT_DIR / "naiveproxy-tui.log"

# ── Utilities ──────────────────────────────────────────────────────

_color_pairs: dict = {}


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    pairs = [
        ("header", curses.COLOR_CYAN, -1),
        ("ok", curses.COLOR_GREEN, -1),
        ("err", curses.COLOR_RED, -1),
        ("warn", curses.COLOR_YELLOW, -1),
        ("dim", curses.COLOR_BLACK, -1),
        ("title", curses.COLOR_WHITE, curses.COLOR_BLUE),
        ("input", curses.COLOR_WHITE, curses.COLOR_BLACK),
        ("hilite", curses.COLOR_BLACK, curses.COLOR_CYAN),
    ]
    for i, (name, fg, bg) in enumerate(pairs, 1):
        curses.init_pair(i, fg, bg if bg != -1 else -1)
        _color_pairs[name] = curses.color_pair(i)


def cp(name):
    return _color_pairs.get(name, curses.A_NORMAL)


def cpf(name, attr=0):
    return cp(name) | attr


def _listen_as_list(data: dict) -> list[str]:
    raw = data.get("listen", [])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return raw
    return []


# ── Configuration Manager ─────────────────────────────────────────

class ConfigManager:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = self._load_default()

    def _load_default(self) -> dict:
        return {
            "listen": ["socks://127.0.0.1:1080"],
            "proxy": "https://user:pass@example.com",
            "log": "",
        }

    def load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.data = self._load_default()
        else:
            self.data = self._load_default()

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2))
            return True
        except OSError:
            return False

    def to_cmd_args(self) -> list[str]:
        args = []
        for listen in _listen_as_list(self.data):
            args.append(f"--listen={listen}")
        proxy = self.data.get("proxy", "")
        if proxy:
            args.append(f"--proxy={proxy}")
        log = self.data.get("log")
        if log:
            args.append(f"--log={log}")
        insecure = self.data.get("insecure-concurrency")
        if insecure:
            args.append(f"--insecure-concurrency={insecure}")
        tunnel_to = self.data.get("tunnel-timeout")
        if tunnel_to:
            args.append(f"--tunnel-timeout={tunnel_to}")
        idle_to = self.data.get("idle-timeout")
        if idle_to:
            args.append(f"--idle-timeout={idle_to}")
        extra = self.data.get("extra-headers")
        if extra:
            args.append(f"--extra-headers={extra}")
        host_resolver = self.data.get("host-resolver-rules")
        if host_resolver:
            args.append(f"--host-resolver-rules={host_resolver}")
        resolver_range = self.data.get("resolver-range")
        if resolver_range:
            args.append(f"--resolver-range={resolver_range}")
        no_pq = self.data.get("no-post-quantum", False)
        if no_pq:
            args.append("--no-post-quantum")
        return args


# ── Naive Process Controller ──────────────────────────────────────

class NaiveController:
    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg
        self.process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self.log_queue: queue.Queue = queue.Queue()
        self.status_text = "stopped"
        self.pid: Optional[int] = None

    @property
    def running(self) -> bool:
        if self.process is None:
            return False
        ret = self.process.poll()
        return ret is None

    def start(self) -> str:
        if self.running:
            return "already_running"
        # Look for binary: first at NAIVE_BIN, then in PATH
        binary = NAIVE_BIN
        if not binary.exists():
            from shutil import which
            path_naive = which("naive")
            if path_naive:
                binary = Path(path_naive)
            else:
                return "no_binary"
        cmd = [str(binary)] + self.cfg.to_cmd_args()
        try:
            self._stop_event.clear()
            self.process = subprocess.Popen(
                cmd,
                cwd=str(NAIVE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            self.pid = self.process.pid
            self._reader_thread = threading.Thread(target=self._reader, daemon=True)
            self._reader_thread.start()
            self.status_text = "running"
            return "ok"
        except OSError as e:
            self.status_text = "error"
            return f"error: {e}"

    def stop(self) -> str:
        if not self.running:
            self.status_text = "stopped"
            return "not_running"
        self._stop_event.set()
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                self.process.wait(timeout=2)
            except OSError:
                pass
        self.process = None
        self.pid = None
        self.status_text = "stopped"
        return "ok"

    def restart(self) -> str:
        self.stop()
        time.sleep(0.3)
        return self.start()

    def _reader(self):
        while not self._stop_event.is_set():
            if self.process and self.process.stdout:
                try:
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    self.log_queue.put(line.decode("utf-8", errors="replace").rstrip())
                except ValueError:
                    break

    def get_logs(self, max_lines=200) -> list[str]:
        lines = []
        while not self.log_queue.empty() and len(lines) < max_lines:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        return lines


# ── Naive Binary Downloader ──────────────────────────────────────

class NaiveDownloader:
    API = "https://api.github.com/repos/klzgrad/naiveproxy/releases/latest"

    ARCH_MAP = {
        ("linux", "x86_64"):   "linux-x64",
        ("linux", "amd64"):    "linux-x64",
        ("linux", "i686"):     "linux-x86",
        ("linux", "i386"):     "linux-x86",
        ("linux", "aarch64"):  "linux-arm64",
        ("linux", "armv8l"):   "linux-arm64",
        ("linux", "armv7l"):   "linux-arm",
        ("linux", "arm"):      "linux-arm",
        ("linux", "riscv64"):  "linux-riscv64",
        ("linux", "loongarch64"): "linux-loong64",
        ("linux", "mips64el"): "linux-mips64el",
        ("linux", "mipsel"):   "linux-mipsel",
        ("darwin", "x86_64"):  "mac-x64-x64",
        ("darwin", "amd64"):   "mac-x64-x64",
        ("darwin", "aarch64"): "mac-arm64-arm64",
        ("darwin", "arm64"):   "mac-arm64-arm64",
    }

    def __init__(self):
        self.status: list[str] = []
        self.done = False
        self.success = False
        self.error = ""
        self._thread: Optional[threading.Thread] = None

    def _detect_asset(self) -> str | None:
        import platform
        sys_name = platform.system().lower()
        machine = platform.machine().lower()
        key = (sys_name, machine)
        suffix = self.ARCH_MAP.get(key)
        if not suffix:
            for (os_name, arch), val in self.ARCH_MAP.items():
                if os_name == sys_name and arch in machine:
                    suffix = val
                    break
        if not suffix:
            self.error = f"Unsupported platform: {sys_name}/{machine}"
            return None
        return suffix

    def download(self, target_path: Path):
        import urllib.request
        import tarfile
        import io

        self.done = False
        self.success = False
        self.status.clear()

        asset_suffix = self._detect_asset()
        if not asset_suffix:
            self.done = True
            return

        def _run():
            try:
                self.status.append("[*] Detecting latest release ...")
                req = urllib.request.Request(self.API,
                    headers={"User-Agent": "naiveproxy-tui/1.0", "Accept": "application/json"})
                resp = urllib.request.urlopen(req, timeout=15)
                rel = json.loads(resp.read().decode())
                tag = rel["tag_name"]

                asset_name = f"naiveproxy-{tag}-{asset_suffix}.tar.xz"
                dl_url = None
                for a in rel.get("assets", []):
                    if a["name"] == asset_name:
                        dl_url = a["browser_download_url"]
                        break
                if not dl_url:
                    self.error = f"Asset not found: {asset_name}"
                    self.done = True
                    self.success = False
                    return

                self.status.append(f"[*] Downloading {tag} ({asset_suffix}) ...")
                dl_req = urllib.request.Request(dl_url,
                    headers={"User-Agent": "naiveproxy-tui/1.0"})
                dl_resp = urllib.request.urlopen(dl_req, timeout=60)
                data = dl_resp.read()
                self.status.append(f"[*] Downloaded {len(data)//1024} KB, extracting ...")

                with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tar:
                    root_dir = f"naiveproxy-{tag}-{asset_suffix}"
                    member = tar.getmember(f"{root_dir}/naive")
                    with tar.extractfile(member) as f:
                        binary_data = f.read()

                target_path.write_bytes(binary_data)
                target_path.chmod(0o755)

                self.success = True
                self.status.append(f"[+] Naive {tag} installed at {target_path}")
                self.done = True

            except Exception as e:
                self.error = str(e)
                self.status.append(f"[!] Download failed: {e}")
                self.done = True
                self.success = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def poll_status(self) -> list[str]:
        return list(self.status)


# ── VPS Deployer ──────────────────────────────────────────────────

class VPSDeployer:
    def __init__(self):
        self.config_path = VPS_CONFIG_FILE
        self.config: dict = self._load()

    def _load(self) -> dict:
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "host": "",
            "port": 22,
            "user": "root",
            "domain": "",
            "email": "",
            "auth_user": "",
            "auth_pass": "",
        }

    def save(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def deploy(self, log_callback) -> bool:
        cfg = self.config
        host = cfg["host"]
        port = cfg["port"]
        user = cfg["user"]
        domain = cfg["domain"]
        email = cfg["email"]
        auser = cfg["auth_user"]
        apass = cfg["auth_pass"]

        if not all([host, domain, email, auser, apass]):
            log_callback("[!] Fill all required fields")
            return False

        log_callback(f"[*] Connecting to {user}@{host}:{port} ...")

        # Use .format() for the script to avoid f-string clash with shell braces
        script_template = """set -e
echo "[*] Installing Caddy ..."
which caddy >/dev/null 2>&1 || {{
  apt-get update -qq
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
  echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
}}

echo "[*] Building Naive fork of forwardproxy ..."
which xcaddy >/dev/null 2>&1 || {{
  apt-get install -y -qq golang-go
  go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
}}
~/go/bin/xcaddy build --with github.com/caddyserver/forwardproxy=github.com/klzgrad/forwardproxy@naive -o /usr/bin/caddy-naive 2>&1

echo "[*] Configuring Caddyfile ..."
cat > /etc/caddy/Caddyfile << 'CADDYEOF'
{{
  order forward_proxy before file_server
  log {{
    exclude http.log.error
  }}
}}
:443, {domain} {{
  tls {email}
  encode
  forward_proxy {{
    basic_auth {auser} {apass}
    hide_ip
    hide_via
    probe_resistance
  }}
  file_server {{
    root /var/www/html
  }}
}}
CADDYEOF

setcap cap_net_bind_service=+ep /usr/bin/caddy-naive
systemctl stop caddy 2>/dev/null || true

cat > /etc/systemd/system/caddy-naive.service << 'UNIT'
[Unit]
Description=Caddy Naive Proxy
Documentation=https://caddyserver.com/docs/
After=network.target

[Service]
Type=notify
User=root
Group=root
ExecStart=/usr/bin/caddy-naive run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/bin/caddy-naive reload --config /etc/caddy/Caddyfile --adapter caddyfile
TimeoutStopSec=5s
LimitNPROC=512
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now caddy-naive
echo "[+] Deployment complete!"
systemctl status caddy-naive --no-pager | head -10
"""
        script = script_template.format(domain=domain, email=email, auser=auser, apass=apass)

        try:
            ssh_cmd = [
                "ssh", f"{user}@{host}", "-p", str(port),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
            ]
            log_callback("[*] Running remote setup (may take a few minutes) ...")
            proc = subprocess.Popen(
                ssh_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout, _ = proc.communicate(input=script.encode(), timeout=300)
            output = stdout.decode("utf-8", errors="replace")
            for line in output.splitlines():
                log_callback(f"  {line}")

            if proc.returncode != 0:
                log_callback(f"[!] SSH exited with code {proc.returncode}")
                return False

            log_callback("[+] VPS Deployment successful!")
            return True

        except subprocess.TimeoutExpired:
            log_callback("[!] Deployment timed out (300s)")
            return False
        except FileNotFoundError:
            log_callback("[!] ssh not found. Install openssh-client.")
            return False
        except Exception as e:
            log_callback(f"[!] Error: {e}")
            return False


# ── TUI Application ───────────────────────────────────────────────

class NaiveTUI:
    PAD_W = 4096

    SCREEN_NAMES = {
        "dashboard": "dashboard",
        "config_editor": "config_editor",
        "process_control": "process_control",
        "log_viewer": "log_viewer",
        "vps_deploy": "vps_deploy",
    }

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.cfg = ConfigManager(CONFIG_FILE)
        self.cfg.load()
        self.ctl = NaiveController(self.cfg)
        self.deployer = VPSDeployer()
        self.downloader = NaiveDownloader()
        self.height, self.width = stdscr.getmaxyx()
        self.log_buffer: list[str] = []
        self.log_pad_pos = 0
        self.status_msg = ""
        self.status_time = 0

        self.menu_items = [
            "dashboard",
            "config_editor",
            "process_control",
            "log_viewer",
            "vps_deploy",
        ]
        self.current_menu = 0

    # ── Helpers ──

    def _draw_header(self, title: str):
        h = self.stdscr
        h.attron(cpf("title"))
        try:
            h.addstr(0, 0, " " * self.width)
        except curses.error:
            pass
        try:
            h.addstr(0, 1, f" NaiveProxy TUI  |  {title}  ")
        except curses.error:
            pass
        h.attroff(cpf("title"))
        if self.ctl.running:
            status = "RUNNING"
            sc = cpf("ok")
        else:
            status = "STOPPED"
            sc = cpf("err")
        st = f" [{status}] "
        h.attron(sc | curses.A_BOLD)
        try:
            h.addstr(0, max(0, self.width - len(st) - 1), st)
        except curses.error:
            pass
        h.attroff(sc | curses.A_BOLD)
        h.refresh()

    def _draw_status_bar(self, msg: str = ""):
        if msg:
            self.status_msg = msg
            self.status_time = time.time()
        h = self.stdscr
        y = self.height - 1
        h.attron(cpf("dim"))
        try:
            h.addstr(y, 0, " " * (self.width - 1))
        except curses.error:
            pass
        dt = time.time() - self.status_time
        if dt < 3:
            try:
                h.addstr(y, 1, self.status_msg[: self.width - 2])
            except curses.error:
                pass
        try:
            h.addstr(y, max(0, self.width - 36), " [Ctrl+Q Back  ←→ Screens]")
        except curses.error:
            pass
        h.attroff(cpf("dim"))
        h.refresh()

    def _draw_menu_bar(self):
        h = self.stdscr
        h.attron(cpf("dim"))
        try:
            h.addstr(self.height - 2, 0, " " * (self.width - 1))
        except curses.error:
            pass
        h.attroff(cpf("dim"))
        labels = {
            "dashboard": " Dashboard ",
            "config_editor": " Config ",
            "process_control": " Process ",
            "log_viewer": " Logs ",
            "vps_deploy": " Deploy ",
        }
        x = 2
        for name in self.menu_items:
            label = labels.get(name, f" {name} ")
            if name == self.menu_items[self.current_menu]:
                h.attron(cpf("hilite") | curses.A_BOLD)
                h.addstr(self.height - 2, x, label)
                h.attroff(cpf("hilite") | curses.A_BOLD)
            else:
                h.addstr(self.height - 2, x, label)
            x += len(label) + 2

    def _clear_content(self):
        for y in range(1, self.height - 3):
            try:
                self.stdscr.attron(cp("input"))
                self.stdscr.addstr(y, 0, " " * (self.width - 1))
                self.stdscr.attroff(cp("input"))
            except curses.error:
                pass

    def _notify(self, msg: str):
        self._draw_status_bar(msg)

    def _input_field(self, y: int, x: int, label: str, value: str,
                     width: int = 0, secret: bool = False) -> str:
        h = self.stdscr
        w = width or (self.width - x - len(label) - 4)
        try:
            h.addstr(y, x, label, cpf("title"))
        except curses.error:
            return value
        fx = x + len(label) + 1
        try:
            h.addstr(y, fx, " " * w, cp("input"))
            display = "*" * len(value) if secret else value
            h.addstr(y, fx, display[:w], cp("input"))
        except curses.error:
            return value
        h.move(y, fx + min(len(value), w - 1))
        edit_win = curses.newwin(1, w, y, fx)
        edit_win.attron(cp("input"))
        tb = curses.textpad.Textbox(edit_win, insert_mode=True)
        edit_win.addstr(0, 0, value[:w])
        edit_win.move(0, min(len(value), w - 1))
        curses.curs_set(1)
        try:
            result = tb.edit(validator=self._edit_validator)
        except TypeError:
            try:
                result = tb.edit()
            except KeyboardInterrupt:
                result = value
        except KeyboardInterrupt:
            result = value
        curses.curs_set(0)
        return result.strip() if result else value

    @staticmethod
    def _edit_validator(ch):
        if ch in (curses.ascii.ESC, curses.ascii.BEL, curses.KEY_ENTER, 10, 13):
            return curses.ascii.BEL
        return ch

    # ── Screen navigation helpers ──

    def _nav_left(self) -> str | None:
        self.current_menu = max(0, self.current_menu - 1)
        return self.menu_items[self.current_menu]

    def _nav_right(self) -> str | None:
        self.current_menu = min(len(self.menu_items) - 1, self.current_menu + 1)
        return self.menu_items[self.current_menu]

    def _nav_tab(self) -> str | None:
        self.current_menu = (self.current_menu + 1) % len(self.menu_items)
        return self.menu_items[self.current_menu]

    def _nav_back(self) -> str:
        self.current_menu = 0
        return "dashboard"

    def binary_download_screen(self) -> str:
        h = self.stdscr
        if self.downloader.done:
            self.downloader = NaiveDownloader()
        self.downloader.download(NAIVE_BIN)
        spinner = "|/-\\"
        sp_idx = 0
        while not self.downloader.done:
            self._draw_header("Download Naive Binary")
            self._clear_content()
            lines = self.downloader.poll_status()
            for i, line in enumerate(lines[-8:]):
                clr = cpf("ok") if line.startswith("[+]") else cpf("err") if line.startswith("[!]") else cp("input")
                try:
                    h.addstr(2 + i, 4, f" {spinner[sp_idx % 4]} {line[:self.width-10]}", clr)
                except curses.error:
                    pass
            sp_idx += 1
            h.refresh()
            self._draw_status_bar()
            self._draw_menu_bar()
            time.sleep(0.12)
        self._draw_header("Download Naive Binary")
        self._clear_content()
        lines = self.downloader.poll_status()
        for i, line in enumerate(lines):
            clr = cpf("ok") if line.startswith("[+]") else cpf("err") if line.startswith("[!]") else cp("input")
            try:
                h.addstr(2 + i, 4, f"  {line[:self.width-10]}", clr)
            except curses.error:
                pass
        if self.downloader.success:
            try:
                h.addstr(2 + len(lines) + 1, 4, " Binary installed! Press any key to continue.", cpf("ok") | curses.A_BOLD)
            except curses.error:
                pass
        else:
            try:
                h.addstr(2 + len(lines) + 1, 4, f" Failed: {self.downloader.error or 'unknown'}", cpf("err") | curses.A_BOLD)
                h.addstr(2 + len(lines) + 2, 4, " Download manually from: https://github.com/klzgrad/naiveproxy/releases", cpf("dim"))
            except curses.error:
                pass
        h.refresh()
        h.getch()
        self.downloader = NaiveDownloader()
        return "dashboard"

    # ── Screens ──

    def dashboard(self) -> str:
        h = self.stdscr
        self._draw_header("Dashboard")
        self._clear_content()
        status_clr = cpf("ok") if self.ctl.running else cpf("err")
        status_txt = "● RUNNING" if self.ctl.running else "● STOPPED"
        h.attron(cpf("title"))
        h.addstr(2, 4, " NaiveProxy Client  ")
        h.attroff(cpf("title"))
        h.addstr(4, 4, "Status:  "); h.addstr(status_txt, status_clr | curses.A_BOLD)
        pid_txt = str(self.ctl.pid) if self.ctl.pid else "-"
        h.addstr(5, 4, f"PID:     {pid_txt}")
        h.addstr(6, 4, f"Config:  {self.cfg.path.resolve()}")
        binary_ok = NAIVE_BIN.exists()
        bin_clr = cpf("ok") if binary_ok else cpf("err")
        bin_txt = str(NAIVE_BIN.resolve()) if binary_ok else "NOT FOUND"
        h.addstr(7, 4, "Binary:  ", cp("input"))
        h.addstr(bin_txt, bin_clr | curses.A_BOLD)

        h.attron(cpf("title"))
        h.addstr(9, 4, " Current Config  ")
        h.attroff(cpf("title"))
        listen_str = ", ".join(_listen_as_list(self.cfg.data))
        h.addstr(10, 4, f"Listen: {listen_str}")
        h.addstr(11, 4, "Proxy:  {}".format(self.cfg.data.get('proxy', '-')))
        log_val = self.cfg.data.get("log", "")
        h.addstr(12, 4, "Log:    {}".format(log_val if log_val else '(console)'))

        y = 14
        actions = [
            ("[1] Start Client", "start"),
            ("[2] Stop Client", "stop"),
            ("[3] Restart Client", "restart"),
            ("[4] Config Editor", "config"),
            ("[5] View Logs", "logs"),
        ]
        if not binary_ok:
            actions.append(("[7] Download Naive Binary", "download"))
        else:
            actions.append(("[6] Deploy to VPS", "vps"))
        for label, action in actions:
            clr = cpf("ok") if "Start" in label or "Deploy" in label else \
                  cpf("warn") if "Download" in label else cp("input")
            h.addstr(y, 6, label, clr | curses.A_BOLD)
            y += 1

        h.refresh()
        while True:
            self._draw_status_bar()
            self._draw_menu_bar()
            curses.flushinp()
            key = h.getch()
            if key in (curses.KEY_LEFT, ord('h')): return self._nav_left()
            if key in (curses.KEY_RIGHT, ord('l')): return self._nav_right()
            if key == 9: return self._nav_tab()
            if key == ord('q'): return None
            if key == ord('1'):
                r = self.ctl.start()
                self._notify({"ok": "Started", "already_running": "Already running", "no_binary": "naive binary not found"}.get(r, r))
            if key == ord('2'):
                r = self.ctl.stop()
                self._notify({"ok": "Stopped", "not_running": "Not running"}.get(r, r))
            if key == ord('3'):
                r = self.ctl.restart()
                self._notify({"ok": "Restarted", "no_binary": "naive binary not found", "already_running": "Already running"}.get(r, r))
            if key == ord('4'): self.current_menu = 1; return "config_editor"
            if key == ord('5'): self.current_menu = 3; return "log_viewer"
            if key == ord('6') and binary_ok: self.current_menu = 4; return "vps_deploy"
            if key == ord('7') and not binary_ok: return "binary_download_screen"

    def config_editor(self) -> str:
        h = self.stdscr
        cfg = self.cfg.data
        listen_list = _listen_as_list(cfg)
        fields = [
            ("listen[0]", "Listen URI 1", listen_list[0] if len(listen_list) > 0 else ""),
            ("listen[1]", "Listen URI 2", listen_list[1] if len(listen_list) > 1 else ""),
            ("proxy", "Proxy URI", cfg.get("proxy", "")),
            ("log", "Log path", cfg.get("log", "")),
            ("insecure-concurrency", "Insecure concurrency", str(cfg.get("insecure-concurrency", ""))),
            ("tunnel-timeout", "Tunnel timeout (s)", str(cfg.get("tunnel-timeout", ""))),
            ("idle-timeout", "Idle timeout (s)", str(cfg.get("idle-timeout", ""))),
            ("extra-headers", "Extra headers", str(cfg.get("extra-headers", ""))),
            ("host-resolver-rules", "Host resolver rules", str(cfg.get("host-resolver-rules", ""))),
            ("resolver-range", "Resolver range", str(cfg.get("resolver-range", ""))),
            ("no-post-quantum", "No post-quantum", str(cfg.get("no-post-quantum", False))),
        ]
        idx = 0
        offset = 0
        max_visible = self.height - 6

        while True:
            self._draw_header("Config Editor")
            self._clear_content()
            visible = fields[offset: offset + max_visible]
            for i, (key, label, val) in enumerate(visible):
                y = 2 + i
                act = i + offset
                prefix = "▸" if act == idx else " "
                clr = cpf("hilite") if act == idx else cp("input")
                display = val if len(val) < self.width - len(label) - 10 else val[:self.width - len(label) - 13] + "..."
                h.addstr(y, 2, f" {prefix} {label}: ", clr)
                h.addstr(f"{display}", cp("input"))
            h.attron(cpf("dim"))
            h.addstr(self.height - 4, 2, " ↑↓/Tab fields  Enter=edit  Tab→next screen  s=save  d=defaults  q=back")
            h.attroff(cpf("dim"))
            h.refresh()
            self._draw_status_bar()
            self._draw_menu_bar()
            curses.flushinp()
            key = h.getch()
            if key in (curses.KEY_UP, ord('k')): idx = max(0, idx - 1); offset = min(offset, idx)
            elif key in (curses.KEY_DOWN, ord('j')): idx = min(len(fields) - 1, idx + 1); offset = max(0, idx - max_visible + 1)
            elif key in (9,):  # Tab → next field, or next screen at end
                if idx >= len(fields) - 1:
                    return self._nav_right()
                idx = min(len(fields) - 1, idx + 1); offset = max(0, idx - max_visible + 1)
            elif key in (curses.KEY_BTAB, 353):  # Shift+Tab → prev field, or prev screen at start
                if idx <= 0:
                    return self._nav_left()
                idx = max(0, idx - 1); offset = min(offset, idx)
            elif key in (10, 13, ord(' ')):
                k, lbl, val = fields[idx]
                new_val = self._input_field(2, 2, lbl, val, width=self.width - len(lbl) - 12)
                fields[idx] = (k, lbl, new_val)
                self._notify(f"{lbl} updated")
            elif key == ord('s'):
                new_listen = []
                v1 = fields[0][2]
                v2 = fields[1][2]
                if v1: new_listen.append(v1)
                if v2: new_listen.append(v2)
                cfg["listen"] = new_listen if new_listen else ["socks://127.0.0.1:1080"]
                cfg["proxy"] = fields[2][2]
                cfg["log"] = fields[3][2]
                for k, lbl, val in fields[4:]:
                    if k == "no-post-quantum":
                        cfg[k] = val.lower() in ("true", "1", "yes")
                    elif k == "extra-headers":
                        cfg[k] = val if val else None
                    elif k == "host-resolver-rules":
                        cfg[k] = val if val else None
                    elif k == "resolver-range":
                        cfg[k] = val if val else None
                    elif val:
                        try:
                            cfg[k] = int(val)
                        except ValueError:
                            cfg[k] = val
                    else:
                        cfg.pop(k, None)
                for k in ("extra-headers", "host-resolver-rules", "resolver-range"):
                    if cfg.get(k) is None:
                        cfg.pop(k, None)
                if self.cfg.save():
                    self._notify("Config saved")
                else:
                    self._notify("Failed to save config")
            elif key == ord('d'):
                self.cfg.data = self.cfg._load_default()
                listen_list = _listen_as_list(cfg)
                fields = [
                    ("listen[0]", "Listen URI 1", listen_list[0] if len(listen_list) > 0 else ""),
                    ("listen[1]", "Listen URI 2", listen_list[1] if len(listen_list) > 1 else ""),
                    ("proxy", "Proxy URI", cfg.get("proxy", "")),
                    ("log", "Log path", cfg.get("log", "")),
                    ("insecure-concurrency", "Insecure concurrency", ""),
                    ("tunnel-timeout", "Tunnel timeout (s)", ""),
                    ("idle-timeout", "Idle timeout (s)", ""),
                    ("extra-headers", "Extra headers", ""),
                    ("host-resolver-rules", "Host resolver rules", ""),
                    ("resolver-range", "Resolver range", ""),
                    ("no-post-quantum", "No post-quantum", "False"),
                ]
                self._notify("Defaults restored")
            elif key in (curses.KEY_LEFT, ord('h')): return self._nav_left()
            elif key in (curses.KEY_RIGHT, ord('l')): return self._nav_right()
            elif key in (ord('q'), 27):
                return "dashboard"

    def process_control(self) -> str:
        h = self.stdscr
        while True:
            self._draw_header("Process Control")
            self._clear_content()
            y = 2
            status_clr = cpf("ok") if self.ctl.running else cpf("err")
            status_txt = "● RUNNING" if self.ctl.running else "● STOPPED"
            h.addstr(y, 4, "Status:     ", cp("title")); h.addstr(status_txt, status_clr | curses.A_BOLD); y += 1
            h.addstr(y, 4, f"PID:        {self.ctl.pid or '-'}"); y += 1
            binary_ok = NAIVE_BIN.exists()
            bin_clr = cpf("ok") if binary_ok else cpf("err")
            bin_txt = str(NAIVE_BIN.resolve()) if binary_ok else "NOT FOUND"
            h.addstr(y, 4, "Binary:     ", cp("input")); h.addstr(bin_txt, bin_clr | curses.A_BOLD); y += 1
            h.addstr(y, 4, f"Config:     {self.cfg.path.resolve()}"); y += 2

            buttons = [
                ("[1]  Start", "start", cpf("ok")),
                ("[2]  Stop", "stop", cpf("err")),
                ("[3]  Restart", "restart", cpf("warn")),
            ]
            if not binary_ok:
                buttons.append(("[4]  Download Binary", "download", cpf("warn")))
            for label, action, clr in buttons:
                h.addstr(y, 6, label, clr | curses.A_BOLD)
                h.addstr(f"  —  {action.capitalize()} naive client" if action != "download" else "  —  Download naiveproxy binary")
                y += 1

            h.attron(cpf("dim"))
            h.addstr(self.height - 4, 4, " 1/2/3/4=action  q=dashboard")
            h.attroff(cpf("dim"))
            h.refresh()
            self._draw_status_bar()
            self._draw_menu_bar()
            curses.flushinp()
            key = h.getch()
            if key == ord('1'):
                r = self.ctl.start()
                self._notify({"ok": "Started", "already_running": "Already running", "no_binary": "naive binary not found"}.get(r, r))
            elif key == ord('2'):
                r = self.ctl.stop()
                self._notify({"ok": "Stopped", "not_running": "Not running"}.get(r, r))
            elif key == ord('3'):
                r = self.ctl.restart()
                self._notify({"ok": "Restarted", "no_binary": "naive binary not found", "already_running": "Already running"}.get(r, r))
            elif key == ord('4') and not binary_ok:
                return "binary_download_screen"
            elif key in (curses.KEY_LEFT, ord('h')): return self._nav_left()
            elif key in (curses.KEY_RIGHT, ord('l')): return self._nav_right()
            elif key in (ord('q'), 27): return self._nav_back()
            elif key == 9: return self._nav_tab()

    def log_viewer(self) -> str:
        h = self.stdscr
        pad = curses.newpad(self.PAD_W, self.width - 2)
        all_logs: list[str] = []
        follow = True

        while True:
            self._draw_header("Log Viewer")
            new_logs = self.ctl.get_logs(500)
            all_logs.extend(new_logs)
            if len(all_logs) > 2000:
                all_logs = all_logs[-1500:]

            pad.clear()
            pad.attron(cp("input"))
            for i, line in enumerate(all_logs):
                if i >= self.PAD_W:
                    break
                safe = line[:self.width - 4]
                try:
                    pad.addstr(i, 0, safe)
                except curses.error:
                    pass
            pad.attroff(cp("input"))

            view_h = self.height - 4
            if follow:
                self.log_pad_pos = max(0, len(all_logs) - view_h)
            try:
                pad.refresh(self.log_pad_pos, 0, 2, 1, self.height - 3, self.width - 2)
            except curses.error:
                pass

            h.attron(cpf("dim"))
            follow_txt = "FOLLOW" if follow else "SCROLL"
            try:
                h.addstr(self.height - 3, 1, f" {follow_txt}  |  Lines: {len(all_logs)}  |  ↑↓ scroll  f=follow  c=clear  q=back")
            except curses.error:
                pass
            h.attroff(cpf("dim"))
            h.refresh()
            self._draw_status_bar()
            self._draw_menu_bar()
            curses.flushinp()
            key = h.getch()
            if key == curses.KEY_UP:
                follow = False
                self.log_pad_pos = max(0, self.log_pad_pos - 1)
            elif key == curses.KEY_DOWN:
                follow = False
                self.log_pad_pos = min(len(all_logs) - 1, self.log_pad_pos + 1)
            elif key == curses.KEY_NPAGE:
                follow = False
                self.log_pad_pos = min(len(all_logs) - 1, self.log_pad_pos + view_h)
            elif key == curses.KEY_PPAGE:
                follow = False
                self.log_pad_pos = max(0, self.log_pad_pos - view_h)
            elif key == ord('f'):
                follow = True
                self._notify("Follow mode ON")
            elif key == ord('c'):
                all_logs.clear()
                self.log_pad_pos = 0
                self._notify("Logs cleared")
            elif key in (curses.KEY_LEFT, ord('h')): return self._nav_left()
            elif key in (curses.KEY_RIGHT, ord('l')): return self._nav_right()
            elif key in (ord('q'), 27): return self._nav_back()
            elif key == 9: return self._nav_tab()

    def vps_deploy(self) -> str:
        h = self.stdscr
        dcfg = self.deployer.config
        fields = [
            ("host", "VPS Host/IP", dcfg.get("host", "")),
            ("port", "SSH Port", str(dcfg.get("port", 22))),
            ("user", "SSH User", dcfg.get("user", "root")),
            ("domain", "Domain", dcfg.get("domain", "")),
            ("email", "Email (TLS)", dcfg.get("email", "")),
            ("auth_user", "Proxy Auth User", dcfg.get("auth_user", "")),
            ("auth_pass", "Proxy Auth Pass", dcfg.get("auth_pass", "")),
        ]
        idx = 0
        offset = 0
        max_visible = max(1, min(len(fields), self.height - 8))
        deploy_output: list[str] = []
        deploying = False

        while True:
            self._draw_header("VPS Deploy")
            self._clear_content()

            y = 2
            h.addstr(y, 4, "Caddy + NaiveProxy Server Deploy", cpf("title") | curses.A_BOLD); y += 2

            visible = fields[offset: offset + max_visible]
            for i, (key, label, val) in enumerate(visible):
                act = i + offset
                prefix = "▸" if act == idx else " "
                clr = cpf("hilite") if act == idx else cp("input")
                display = val if len(val) < self.width - len(label) - 10 else val[:self.width - len(label) - 13] + "..."
                h.addstr(y + i, 4, f" {prefix} {label}: ", clr)
                h.addstr(display, cp("input"))

            out_y = y + max_visible + 1
            if deploy_output:
                for j, line in enumerate(deploy_output[-8:]):
                    if out_y + j < self.height - 4:
                        safe = line[:self.width - 4]
                        try:
                            h.addstr(out_y + j, 4, safe[:self.width - 6])
                        except curses.error:
                            pass

            h.attron(cpf("dim"))
            try:
                h.addstr(self.height - 4, 2, " ↑↓/Tab fields  Enter=edit  Tab→next screen  d=deploy  s=save  q=back")
            except curses.error:
                pass
            h.attroff(cpf("dim"))
            h.refresh()
            self._draw_status_bar()
            self._draw_menu_bar()
            curses.flushinp()
            key = h.getch()
            if key == curses.KEY_UP:
                idx = max(0, idx - 1)
                offset = min(offset, idx)
            elif key == curses.KEY_DOWN:
                idx = min(len(fields) - 1, idx + 1)
                offset = max(0, idx - max_visible + 1)
            elif key in (9,):  # Tab → next field, or next screen at end
                if idx >= len(fields) - 1:
                    return self._nav_right()
                idx = min(len(fields) - 1, idx + 1); offset = max(0, idx - max_visible + 1)
            elif key in (curses.KEY_BTAB, 353):  # Shift+Tab → prev field, or prev screen at start
                if idx <= 0:
                    return self._nav_left()
                idx = max(0, idx - 1); offset = min(offset, idx)
            elif key in (10, 13, ord(' ')):
                k, lbl, val = fields[idx]
                secret = k == "auth_pass"
                new_val = self._input_field(2 + idx - offset, 4, lbl, val,
                                            width=self.width - len(lbl) - 12, secret=secret)
                fields[idx] = (k, lbl, new_val)
                dcfg[k] = int(new_val) if k == "port" else new_val
                self._notify(f"{lbl} updated")
            elif key == ord('s'):
                dcfg["port"] = int(fields[1][2])
                for k, _, v in fields:
                    dcfg[k] = int(v) if k == "port" else v
                self.deployer.save()
                self._notify("VPS config saved")
            elif key == ord('d') and not deploying:
                dcfg["port"] = int(fields[1][2])
                for k, _, v in fields:
                    dcfg[k] = int(v) if k == "port" else v
                self.deployer.save()
                deploying = True
                deploy_output = ["[*] Starting deployment ..."]

                def log_cb(msg):
                    deploy_output.append(msg)

                def deploy_thread():
                    nonlocal deploying
                    try:
                        self.deployer.deploy(log_cb)
                    finally:
                        deploying = False

                t = threading.Thread(target=deploy_thread, daemon=True)
                t.start()

            elif key in (curses.KEY_LEFT, ord('h')): return self._nav_left()
            elif key in (curses.KEY_RIGHT, ord('l')): return self._nav_right()
            elif key in (ord('q'), 27): return "dashboard"

    # ── Event loop ──

    def run(self):
        self.stdscr.clear()
        curses.curs_set(0)
        curses.raw()
        self.stdscr.keypad(True)
        self.height, self.width = self.stdscr.getmaxyx()
        init_colors()

        screen = "dashboard"
        screen_map = {
            "dashboard": self.dashboard,
            "config_editor": self.config_editor,
            "process_control": self.process_control,
            "log_viewer": self.log_viewer,
            "vps_deploy": self.vps_deploy,
            "binary_download_screen": self.binary_download_screen,
        }

        while screen:
            # Update terminal size on each iteration
            try:
                self.height, self.width = self.stdscr.getmaxyx()
            except curses.error:
                pass
            handler = screen_map.get(screen)
            if handler:
                screen = handler()
            else:
                break

    def do_quit(self):
        return None


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("Usage: naiveproxy-tui [config.json]")
        print("  TUI for NaïveProxy management and VPS deployment.")
        sys.exit(0)

    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.exists():
            global CONFIG_FILE
            CONFIG_FILE = p
        else:
            print(f"Config not found: {p}")
            sys.exit(1)

    try:
        curses.wrapper(lambda stdscr: NaiveTUI(stdscr).run())
    except KeyboardInterrupt:
        pass
    print("\nGoodbye.")


if __name__ == "__main__":
    main()

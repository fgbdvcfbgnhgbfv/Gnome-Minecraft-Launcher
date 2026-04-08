import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, GdkPixbuf, Gdk
import threading
import os
import json
import subprocess
import shutil
import urllib.request
import urllib.parse
import urllib.error
import urllib.response
import platform
import zipfile
import tarfile
import tempfile
import time
import re
import minecraft_launcher_lib

# ─── КОНСТАНТЫ ────────────────────────────────────────────────────────────────

MODRINTH_API     = "https://api.modrinth.com/v2"
GITHUB_REPO      = "fgbdvcfbgnhgbfv/Gnome-Minecraft-Launcher"
APP_VERSION      = "2.0.3"

APP_ID           = "com.github.mc_gnome_launcher"

ADOPTIUM_API     = "https://api.adoptium.net/v3/assets/latest/{feature_version}/hotspot"
JAVA_REQUIRED    = {
    "1.21": 21, "1.20": 21, "1.19": 17, "1.18": 17, "1.17": 17,
    "1.16": 8,  "1.15": 8,  "1.14": 8,  "1.13": 8,
    "1.12": 8,  "1.11": 8,  "1.10": 8,  "1.9":  8,
    "1.8":  8,  "1.7":  8,  "b1":   8,  "a1":   8,
}

OPTIFINE_KNOWN = {
    "1.20.1": "HD_U_I6", "1.20": "HD_U_I4", "1.19.4": "HD_U_H9",
    "1.19.2": "HD_U_H9", "1.18.2": "HD_U_H7", "1.17.1": "HD_U_G9",
    "1.16.5": "HD_U_G8", "1.12.2": "HD_U_E3", "1.8.9": "HD_U_M5",
    "1.7.10": "HD_U_E7",
}

# Кеш версий
_versions_cache = None
_versions_cache_time = 0
VERSIONS_CACHE_TTL = 300


# ─── FLATPAK: пути и xdg-open ─────────────────────────────────────────────────

def is_flatpak():
    return os.path.exists("/.flatpak-info")


def get_base_dir():
    """Возвращает корневую папку данных лаунчера.
    В Flatpak: ~/.var/app/<id>/data/gnome-mc-launcher
    Вне Flatpak: ~/.local/share/gnome-mc-launcher
    """
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return os.path.join(xdg, "gnome-mc-launcher")
    return os.path.expanduser("~/.local/share/gnome-mc-launcher")


def open_uri(uri):
    """Открывает URL или папку. В Flatpak использует portal через gio."""
    try:
        if is_flatpak():
            # gio open работает внутри Flatpak через XDG portal
            subprocess.Popen(["gio", "open", uri],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", uri],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[OPEN_URI] {e}")


# ─── ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ ─────────────────────────────────────────────────

def safe_request(url, timeout=10, headers=None):
    """Безопасный HTTP-запрос с обработкой ошибок."""
    h = {"User-Agent": f"gnome-mc-launcher/{APP_VERSION}"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        raise Exception(f"HTTP {e.code}: {e.reason} — {url}")
    except urllib.error.URLError as e:
        raise Exception(f"Сеть недоступна: {e.reason}")
    except Exception as e:
        raise Exception(f"Запрос не удался: {e}")


def download_file(url, dest_path, progress_cb=None, timeout=60):
    """Скачивает файл в dest_path с отслеживанием прогресса."""
    h = {"User-Agent": f"gnome-mc-launcher/{APP_VERSION}"}
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb(downloaded / total)
    except urllib.error.HTTPError as e:
        raise Exception(f"HTTP {e.code} при скачивании: {url}")
    except urllib.error.URLError as e:
        raise Exception(f"Сетевая ошибка: {e.reason}")


# ─── KEYRING-СОВМЕСТИМОЕ ХРАНИЛИЩЕ ───────────────────────────────────────────

def _keyring_available():
    try:
        import keyring  # noqa
        return True
    except ImportError:
        return False


def save_token_secure(service, key, value):
    if _keyring_available():
        import keyring
        keyring.set_password(service, key, value)
        return True
    return False


def load_token_secure(service, key):
    if _keyring_available():
        import keyring
        return keyring.get_password(service, key)
    return None


def save_account(config_dir, account_data):
    path = os.path.join(config_dir, "account.json")
    safe_data = {k: v for k, v in account_data.items() if k != "access_token"}
    token = account_data.get("access_token", "")
    if token:
        stored_in_keyring = save_token_secure("gnome-mc-launcher", "access_token", token)
        if not stored_in_keyring:
            safe_data["access_token"] = token
            print("[WARN] keyring недоступен — токен сохранён в файле.")
    with open(path, "w") as f:
        json.dump(safe_data, f, indent=2)


def load_account(config_dir):
    path = os.path.join(config_dir, "account.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        token = load_token_secure("gnome-mc-launcher", "access_token")
        if token:
            data["access_token"] = token
        return data
    except Exception as e:
        print(f"[ACCOUNT] Ошибка загрузки: {e}")
        return None


# ─── MODRINTH API ─────────────────────────────────────────────────────────────

def modrinth_search(query, mc_version=None, loader=None, limit=20):
    params = {"query": query, "limit": limit}
    facets = []
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if loader and loader.lower() not in ("vanilla",):
        facets.append([f"categories:{loader.lower()}"])
    url = f"{MODRINTH_API}/search?" + urllib.parse.urlencode(params)
    if facets:
        url += "&facets=" + urllib.parse.quote(json.dumps(facets))
    data, _ = safe_request(url, timeout=12)
    return json.loads(data)["hits"]


def modrinth_get_versions(project_id, mc_version=None, loader=None):
    url = f"{MODRINTH_API}/project/{project_id}/version"
    params = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    if loader and loader.lower() not in ("vanilla",):
        params["loaders"] = json.dumps([loader.lower()])
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data, _ = safe_request(url, timeout=12)
    return json.loads(data)


def modrinth_resolve_dependencies(version_data, mc_version=None, loader=None):
    collected = []
    seen_projects = set()

    def resolve(vdata):
        for dep in vdata.get("dependencies", []):
            if dep.get("dependency_type") != "required":
                continue
            dep_pid = dep.get("project_id")
            dep_vid = dep.get("version_id")
            if not dep_pid or dep_pid in seen_projects:
                continue
            seen_projects.add(dep_pid)
            try:
                if dep_vid:
                    dep_data, _ = safe_request(f"{MODRINTH_API}/version/{dep_vid}", timeout=10)
                    dep_vdata = json.loads(dep_data)
                else:
                    dep_versions = modrinth_get_versions(dep_pid, mc_version, loader)
                    if not dep_versions:
                        continue
                    dep_vdata = dep_versions[0]
                if dep_vdata.get("files"):
                    fi = dep_vdata["files"][0]
                    collected.append({
                        "project_id": dep_pid,
                        "filename": fi["filename"],
                        "url": fi["url"],
                    })
                resolve(dep_vdata)
            except Exception as e:
                print(f"[DEPS] {dep_pid}: {e}")

    resolve(version_data)
    return collected


# ─── JAVA MANAGER ─────────────────────────────────────────────────────────────

def get_required_java_version(mc_version: str) -> int:
    for prefix, java_ver in sorted(JAVA_REQUIRED.items(), key=lambda x: -len(x[0])):
        if mc_version.startswith(prefix):
            return java_ver
    return 17


def get_system_info():
    system  = platform.system().lower()
    machine = platform.machine().lower()
    os_map   = {"linux": "linux", "windows": "windows", "darwin": "mac"}
    arch_map = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}
    return os_map.get(system, "linux"), arch_map.get(machine, "x64")


def find_java_in_launcher(java_dir: str, feature_version: int):
    jdir = os.path.join(java_dir, f"jre{feature_version}")
    if not os.path.isdir(jdir):
        return None
    for root, dirs, files in os.walk(jdir):
        for f in files:
            if f in ("java", "java.exe"):
                return os.path.join(root, f)
    return None


def download_jre(java_dir: str, feature_version: int, status_cb=None, progress_cb=None) -> str:
    os_name, arch = get_system_info()
    dest_dir = os.path.join(java_dir, f"jre{feature_version}")
    os.makedirs(dest_dir, exist_ok=True)

    url = (f"https://api.adoptium.net/v3/binary/latest/{feature_version}/ga/"
           f"{os_name}/{arch}/jre/hotspot/normal/eclipse")
    if status_cb:
        status_cb(f"Скачивание Java {feature_version} ({arch})...")

    is_windows = os_name == "windows"
    ext = ".zip" if is_windows else ".tar.gz"
    archive_path = os.path.join(dest_dir, f"jre{ext}")

    try:
        download_file(url, archive_path, progress_cb=progress_cb, timeout=300)
    except Exception as e:
        raise Exception(f"Не удалось скачать JRE {feature_version}: {e}")

    if status_cb:
        status_cb(f"Распаковка Java {feature_version}...")
    try:
        if is_windows:
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(dest_dir)
        else:
            with tarfile.open(archive_path, "r:gz") as t:
                # FIX: filter='data' prevents path traversal and fixes Python 3.12 warning
                t.extractall(dest_dir, filter="data")
    except Exception as e:
        raise Exception(f"Ошибка распаковки JRE: {e}")
    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)

    java_exec = find_java_in_launcher(java_dir, feature_version)
    if java_exec:
        os.chmod(java_exec, 0o755)
        return java_exec
    raise Exception(f"java не найдена после распаковки JRE {feature_version}")


def get_or_download_java(java_dir: str, mc_version: str, java_path_custom: str = "",
                         status_cb=None, progress_cb=None) -> str:
    if java_path_custom and os.path.isfile(java_path_custom):
        return java_path_custom

    feature_version = get_required_java_version(mc_version)
    java_exec = find_java_in_launcher(java_dir, feature_version)
    if java_exec:
        return java_exec

    # В Flatpak системная java недоступна — сразу качаем
    if not is_flatpak():
        system_java = shutil.which("java")
        if system_java:
            try:
                result = subprocess.run(
                    [system_java, "-version"],
                    capture_output=True, text=True, timeout=5
                )
                output = result.stderr + result.stdout
                match = re.search(r'version "(\d+)', output)
                if match and int(match.group(1)) >= feature_version:
                    return system_java
            except Exception:
                pass

    return download_jre(java_dir, feature_version, status_cb, progress_cb)


# ─── МОДИФИКАЦИИ: ЧТЕНИЕ ИНФО ─────────────────────────────────────────────────

def read_mod_info(jar_path):
    info = {
        "mc_versions": [],
        "loaders": [],
        "name": os.path.basename(jar_path),
        "id": None,
    }
    try:
        with zipfile.ZipFile(jar_path, "r") as z:
            names = set(z.namelist())

            if "fabric.mod.json" in names:
                data = json.loads(z.read("fabric.mod.json"))
                mc = data.get("depends", {}).get("minecraft", "")
                info["loaders"] = ["fabric"]
                info["id"] = data.get("id")
                info["mc_versions"] = [mc] if isinstance(mc, str) else list(mc)

            elif "quilt.mod.json" in names:
                data = json.loads(z.read("quilt.mod.json"))
                info["loaders"] = ["quilt", "fabric"]
                info["id"] = data.get("quilt_loader", {}).get("id")
                for dep in data.get("quilt_loader", {}).get("depends", []):
                    if isinstance(dep, dict) and dep.get("id") == "minecraft":
                        v = dep.get("versions", "")
                        info["mc_versions"] = [v] if isinstance(v, str) else list(v)

            elif "META-INF/neoforge.mods.toml" in names:
                info["loaders"] = ["neoforge", "forge"]
                raw = z.read("META-INF/neoforge.mods.toml").decode("utf-8", errors="ignore")
                for line in raw.splitlines():
                    if "minecraft" in line.lower() and "versionrange" in line.lower() and "=" in line:
                        val = line.split("=", 1)[-1].strip().strip('"').strip("'")
                        info["mc_versions"] = [val]
                        break

            elif "META-INF/mods.toml" in names:
                raw = z.read("META-INF/mods.toml").decode("utf-8", errors="ignore")
                loader_type = "neoforge" if "neoforge" in raw.lower() else "forge"
                info["loaders"] = [loader_type, "forge"]
                for line in raw.splitlines():
                    if "minecraft" in line.lower() and "versionrange" in line.lower() and "=" in line:
                        val = line.split("=", 1)[-1].strip().strip('"').strip("'")
                        info["mc_versions"] = [val]
                        break

    except zipfile.BadZipFile:
        print(f"[MOD INFO] Не архив: {jar_path}")
    except Exception as e:
        print(f"[MOD INFO] {jar_path}: {e}")
    return info


def check_mod_compatibility(jar_path, mc_version, loader):
    info = read_mod_info(jar_path)
    if not info["loaders"]:
        return True, ""
    mod_loaders = [l.lower() for l in info["loaders"]]
    current_loader = loader.lower()
    if current_loader not in ("vanilla",):
        compat_map = {
            "neoforge": ["neoforge", "forge"],
            "quilt":    ["quilt", "fabric"],
            "fabric":   ["fabric"],
            "forge":    ["forge", "neoforge"],
        }
        allowed = compat_map.get(current_loader, [current_loader])
        if not any(ml in allowed for ml in mod_loaders):
            return False, f"Загрузчик: нужен {' или '.join(mod_loaders)}, выбран {loader}"

    if info["mc_versions"] and mc_version:
        versions_raw = " ".join(info["mc_versions"])
        flexible = any(c in versions_raw for c in ["*", ">", "<", ">=", "<=", "[", "]", "||", "^", "~", "x"])
        if not flexible and mc_version not in versions_raw:
            return False, f"MC версия: мод для {versions_raw[:50]}"
    return True, ""


def get_optifine_versions():
    return list(OPTIFINE_KNOWN.keys())


def get_optifine_download_url(mc_version):
    suffix = OPTIFINE_KNOWN.get(mc_version)
    if not suffix:
        return None, None
    filename = f"OptiFine_{mc_version}_{suffix}.jar"
    return f"https://optifine.net/downloadx?f={filename}&x=adloadx", filename


# ─── MICROSOFT AUTH ───────────────────────────────────────────────────────────

MSA_CLIENT_ID = os.environ.get("MC_LAUNCHER_CLIENT_ID", "")


def try_microsoft_login_browser(redirect_port=8765):
    if not MSA_CLIENT_ID:
        raise Exception(
            "Azure Client ID не настроен.\n"
            "Зарегистрируйте приложение на portal.azure.com и\n"
            "укажите MC_LAUNCHER_CLIENT_ID в переменных среды."
        )
    login_url, state, code_verifier = minecraft_launcher_lib.microsoft_account.get_secure_login_data(
        MSA_CLIENT_ID, f"http://localhost:{redirect_port}"
    )
    open_uri(login_url)

    import http.server, socketserver

    received_code  = [None]
    received_state = [None]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            received_code[0]  = params.get("code",  [None])[0]
            received_state[0] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>✓ Вход выполнен! Окно можно закрыть.</h2></body></html>"
                .encode("utf-8")
            )
        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("", redirect_port), Handler) as httpd:
        httpd.timeout = 120
        httpd.handle_request()

    if not received_code[0]:
        raise Exception("Код авторизации не получен. Попробуйте ещё раз.")

    login_data = minecraft_launcher_lib.microsoft_account.parse_auth_code_url(
        f"http://localhost:{redirect_port}?code={received_code[0]}&state={received_state[0]}",
        state,
    )
    account = minecraft_launcher_lib.microsoft_account.complete_login(
        MSA_CLIENT_ID, None, f"http://localhost:{redirect_port}", login_data, code_verifier
    )
    return account["access_token"], account["id"], account["name"]


# ─── КОНФИГ ЛАУНЧЕРА ─────────────────────────────────────────────────────────

class LauncherConfig:
    _DEFAULTS = {
        "offline_username": "Player",
        "ms_username": "",
        "version": "",
        "loader_idx": 0,
        "ram": 4,
        "java_path": "",
        "show_alpha": True,
        "show_snap": False,
        "show_optifine": True,
        "last_instance": "Vanilla (Default)",
    }

    def __init__(self, path: str):
        self.path = path
        self._data = dict(self._DEFAULTS)
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    saved = json.load(f)
                self._data.update({k: saved[k] for k in self._DEFAULTS if k in saved})
            except Exception as e:
                print(f"[CONFIG] Ошибка загрузки: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            print(f"[CONFIG] Ошибка сохранения: {e}")

    def __getitem__(self, key):
        return self._data.get(key, self._DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self._data[key] = value


# ─── ГЛАВНОЕ ОКНО ─────────────────────────────────────────────────────────────

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Gnome Minecraft Launcher")
        self.set_default_size(720, 950)

        self._game_running = False

        # FIX: корректные пути для Flatpak через XDG_DATA_HOME
        self.base_dir      = get_base_dir()
        self.instances_dir = os.path.join(self.base_dir, "instances")
        self.java_dir      = os.path.join(self.base_dir, "java")
        os.makedirs(self.instances_dir, exist_ok=True)
        os.makedirs(self.java_dir, exist_ok=True)

        self.config = LauncherConfig(os.path.join(self.base_dir, "config.json"))
        self.ms_account = load_account(self.base_dir)

        self.stack = Adw.ViewStack()
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.stack)
        self.set_content(self.toast_overlay)

        self.connect("close-request", self._on_close_request)

        # FIX: _install_icon_once убран — в Flatpak иконка ставится манифестом,
        # а вне Flatpak этот код всё равно вызывал гонку при первом запуске
        self.init_settings_page()
        self.init_main_page()
        self.init_modrinth_page()
        self.init_console_page()
        self.init_assets_page()

        self.stack.set_visible_child_name("main")
        self._apply_config_to_ui()
        self._auto_optimize_specs()
        self.load_versions()
        self.refresh_instances()

    # ─── ЗАКРЫТИЕ ─────────────────────────────────────────────────────────────

    def _on_close_request(self, window):
        return False

    # ─── TOAST ────────────────────────────────────────────────────────────────

    def show_toast(self, message, timeout=4):
        def _do():
            toast = Adw.Toast.new(message)
            toast.set_timeout(timeout)
            self.toast_overlay.add_toast(toast)
            return False
        GLib.idle_add(_do)

    # ─── КОНФИГ: ПРИМЕНЕНИЕ К UI ─────────────────────────────────────────────

    def _apply_config_to_ui(self):
        if self.ms_account:
            self.account_entry.set_text(self.ms_account.get("username", "Player"))
        else:
            saved_nick = self.config["offline_username"]
            self.account_entry.set_text(saved_nick if saved_nick else "Player")

        self.ram_spin.set_value(self.config["ram"])
        java_path = self.config["java_path"]
        if java_path:
            self.java_entry.set_text(java_path)

        self.show_alpha.set_active(self.config["show_alpha"])
        self.show_snap.set_active(self.config["show_snap"])
        self.show_optifine.set_active(self.config["show_optifine"])

        self._last_version  = self.config["version"] or None
        self._last_loader   = self.config["loader_idx"]
        self._last_instance = self.config["last_instance"]

    def save_launcher_config(self):
        try:
            offline_nick = self.account_entry.get_text().strip()
            if not self.ms_account:
                self.config["offline_username"] = offline_nick or "Player"

            item = self.version_dropdown.get_selected_item()
            self.config["version"]    = item.get_string() if item else ""
            self.config["loader_idx"] = self.loader_choice.get_selected()
            self.config["ram"]        = int(self.ram_spin.get_value())
            self.config["java_path"]  = self.java_entry.get_text().strip()
            self.config["show_alpha"]    = self.show_alpha.get_active()
            self.config["show_snap"]     = self.show_snap.get_active()
            self.config["show_optifine"] = self.show_optifine.get_active()

            inst_item = self.instance_dropdown.get_selected_item()
            if inst_item:
                self.config["last_instance"] = inst_item.get_string()

            if self.ms_account:
                self.config["ms_username"] = self.ms_account.get("username", "")

            self.config.save()
        except Exception as e:
            print(f"[CONFIG] save_launcher_config: {e}")

    def _auto_optimize_specs(self):
        """Авто-выбор RAM при первом запуске."""
        if not os.path.exists(self.config.path):
            try:
                import psutil
                total_ram = psutil.virtual_memory().total / (1024 ** 3)
                suggested = max(2, min(int(total_ram / 2), 8))
                self.ram_spin.set_value(suggested)
            except ImportError:
                pass  # psutil необязателен

    # ─── АККАУНТ (MS Auth UI) ─────────────────────────────────────────────────

    def _update_account_ui(self):
        if self.ms_account:
            uname = self.ms_account.get("username", "")
            self.account_entry.set_text(uname)
            self.account_entry.set_sensitive(False)
            self.ms_login_btn.set_label("Выйти из Microsoft")
            self.ms_login_btn.remove_css_class("suggested-action")
            self.ms_login_btn.add_css_class("destructive-action")
            self.ms_status_label.set_text(f"✓ Microsoft: {uname}")
        else:
            self.account_entry.set_text(self.config["offline_username"] or "Player")
            self.account_entry.set_sensitive(True)
            self.ms_login_btn.set_label("Войти через Microsoft")
            self.ms_login_btn.remove_css_class("destructive-action")
            self.ms_login_btn.add_css_class("suggested-action")
            self.ms_status_label.set_text("Офлайн-режим (лицензионные серверы недоступны)")

    def on_ms_login_clicked(self, btn):
        if self.ms_account:
            self.config["ms_username"] = self.ms_account.get("username", "")
            self.config.save()
            self.ms_account = None
            account_path = os.path.join(self.base_dir, "account.json")
            if os.path.exists(account_path):
                os.remove(account_path)
            if _keyring_available():
                try:
                    import keyring
                    keyring.delete_password("gnome-mc-launcher", "access_token")
                except Exception:
                    pass
            self._update_account_ui()
            self.show_toast("Вы вышли из аккаунта Microsoft")
            return

        if not MSA_CLIENT_ID:
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Microsoft авторизация",
                body=(
                    "Для входа через Microsoft необходимо зарегистрировать\n"
                    "Azure Application и указать Client ID.\n\n"
                    "1. Перейдите на portal.azure.com\n"
                    "2. Создайте приложение Azure AD\n"
                    "3. Укажите redirect URI: http://localhost:8765\n"
                    "4. Установите переменную среды:\n"
                    "   MC_LAUNCHER_CLIENT_ID=ваш-client-id\n\n"
                    "Подробнее: github.com/JakobDev/minecraft-launcher-lib"
                ),
            )
            dialog.add_response("ok", "Понятно")
            dialog.present()
            return

        btn.set_sensitive(False)
        btn.set_label("Ожидание...")

        def do_login():
            try:
                token, uuid, username = try_microsoft_login_browser()
                account_data = {"access_token": token, "uuid": uuid, "username": username}
                save_account(self.base_dir, account_data)
                self.ms_account = account_data
                self.config["ms_username"] = username
                self.config.save()
                GLib.idle_add(self._update_account_ui)
                self.show_toast(f"✓ Вошли как {username}")
            except Exception as e:
                self.show_toast(f"Ошибка входа: {str(e)[:80]}")
                GLib.idle_add(self._update_account_ui)
            GLib.idle_add(btn.set_sensitive, True)

        threading.Thread(target=do_login, daemon=True).start()

    # ─── ГЛАВНАЯ СТРАНИЦА ─────────────────────────────────────────────────────

    def init_main_page(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()

        icon_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icon.png")
        )
        if os.path.exists(icon_path):
            img_header = Gtk.Image.new_from_file(icon_path)
            img_header.set_pixel_size(24)
            header.set_title_widget(img_header)
        else:
            lbl = Gtk.Label(label="Gnome MC Launcher")
            lbl.add_css_class("heading")
            header.set_title_widget(lbl)

        menu = Gio.Menu()
        menu.append("Ресурсы и Миры", "app.assets")
        menu.append("Консоль",        "app.console")
        menu.append("Настройки",      "app.settings")
        menu.append("О программе",    "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)
        main_box.append(header)

        for name, page, cb in [
            ("settings", "settings", None),
            ("about",    None,       self.on_about_clicked),
            ("assets",   "assets",   None),
            ("console",  "console",  None),
        ]:
            action = Gio.SimpleAction.new(name, None)
            if page:
                action.connect("activate", lambda *_, p=page: self.stack.set_visible_child_name(p))
            elif cb:
                action.connect("activate", cb)
            self.get_application().add_action(action)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        clamp = Adw.Clamp(maximum_size=560)
        scrolled.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(20)
        content.set_margin_end(20)
        clamp.set_child(content)

        # ── Аккаунт ──────────────────────────────────────────────────────────
        acc_group = Adw.PreferencesGroup(title="Профиль")

        self.account_entry = Adw.EntryRow(title="Никнейм (офлайн)")
        self.account_entry.set_text("Player")
        self.account_entry.connect("changed", self._on_offline_nick_changed)
        acc_group.add(self.account_entry)

        ms_row = Adw.ActionRow(
            title="Microsoft аккаунт",
            subtitle="Для лицензионных серверов (Hypixel и др.)",
        )
        self.ms_status_label = Gtk.Label(opacity=0.6, wrap=True)
        self.ms_status_label.set_halign(Gtk.Align.START)
        self.ms_login_btn = Gtk.Button(label="Войти через Microsoft", valign=Gtk.Align.CENTER)
        self.ms_login_btn.add_css_class("suggested-action")
        self.ms_login_btn.connect("clicked", self.on_ms_login_clicked)
        ms_row.add_suffix(self.ms_login_btn)
        acc_group.add(ms_row)

        ms_status_row = Adw.ActionRow()
        ms_status_row.add_prefix(self.ms_status_label)
        ms_status_row.set_activatable(False)
        acc_group.add(ms_status_row)
        content.append(acc_group)

        self._update_account_ui()

        # ── Параметры запуска ─────────────────────────────────────────────────
        game_group = Adw.PreferencesGroup(title="Параметры запуска")

        self.version_model = Gtk.StringList()
        self.version_dropdown = Gtk.DropDown(model=self.version_model)
        ver_row = Adw.ActionRow(title="Версия")
        ver_row.add_suffix(self.version_dropdown)
        game_group.add(ver_row)

        self.loader_choice = Gtk.DropDown.new_from_strings(
            ["Vanilla", "Fabric", "Forge", "Quilt", "NeoForge"]
        )
        loader_row = Adw.ActionRow(title="Движок")
        loader_row.add_suffix(self.loader_choice)
        game_group.add(loader_row)

        self.java_status_row = Adw.ActionRow(
            title="Java",
            subtitle="Будет определена автоматически",
        )
        java_icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        java_icon.set_pixel_size(16)
        self.java_status_row.add_prefix(java_icon)
        game_group.add(self.java_status_row)

        self.version_dropdown.connect("notify::selected", self._on_version_changed)
        content.append(game_group)

        # ── Сборки ───────────────────────────────────────────────────────────
        inst_group = Adw.PreferencesGroup(title="Сборка и Моды")
        self.instance_model = Gtk.StringList.new(["Vanilla (Default)"])
        self.instance_dropdown = Gtk.DropDown(model=self.instance_model)
        self.instance_dropdown.connect("notify::selected", self._on_instance_changed)
        inst_row = Adw.ActionRow(title="Выбрать сборку")
        inst_row.add_suffix(self.instance_dropdown)
        inst_group.add(inst_row)

        inst_ctrl = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        inst_ctrl.add_css_class("linked")
        for icon, tip, cb in [
            ("list-add-symbolic",      "Создать сборку",          self.on_create_instance_clicked),
            ("folder-open-symbolic",   "Добавить моды (.jar)",    self.on_add_mod_clicked),
            ("system-search-symbolic", "Найти моды на Modrinth",  self.on_open_modrinth),
            ("user-trash-symbolic",    "Удалить сборку",          self.on_delete_instance_clicked),
        ]:
            btn = Gtk.Button(icon_name=icon)
            btn.set_tooltip_text(tip)
            btn.connect("clicked", cb)
            inst_ctrl.append(btn)
        inst_group.add(inst_ctrl)
        content.append(inst_group)

        self.mods_listbox = Gtk.ListBox()
        self.mods_listbox.add_css_class("boxed-list")
        self.mods_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        content.append(self.mods_listbox)

        # ── Запуск ───────────────────────────────────────────────────────────
        self.launch_btn = Gtk.Button(label="▶  Играть")
        self.launch_btn.add_css_class("suggested-action")
        self.launch_btn.add_css_class("pill")
        self.launch_btn.set_size_request(-1, 60)
        self.launch_btn.connect("clicked", self.on_launch_clicked)
        content.append(self.launch_btn)

        self.progress_bar = Gtk.ProgressBar(visible=False)
        content.append(self.progress_bar)
        self.status_label = Gtk.Label(label="Готов", opacity=0.6)
        content.append(self.status_label)

        self.stack.add_named(main_box, "main")

    def _on_offline_nick_changed(self, entry):
        if not self.ms_account:
            nick = entry.get_text().strip()
            if nick:
                self.config["offline_username"] = nick
                self.config.save()

    def _on_version_changed(self, *_):
        item = self.version_dropdown.get_selected_item()
        if not item:
            return
        ver = item.get_string().replace(" + OptiFine", "")
        java_ver = get_required_java_version(ver)
        java_exec = find_java_in_launcher(self.java_dir, java_ver)
        if java_exec:
            self.java_status_row.set_subtitle(f"Java {java_ver} (встроенная): {java_exec}")
        elif not is_flatpak() and shutil.which("java"):
            self.java_status_row.set_subtitle(f"Java {java_ver} — системная: {shutil.which('java')}")
        else:
            self.java_status_row.set_subtitle(
                f"Java {java_ver} — будет скачана автоматически при запуске (~60 МБ)"
            )

    def _on_instance_changed(self, *_):
        inst_item = self.instance_dropdown.get_selected_item()
        if inst_item:
            self.config["last_instance"] = inst_item.get_string()
            self.config.save()
        self.refresh_mods_list()

    def on_open_modrinth(self, *_):
        self.stack.set_visible_child_name("modrinth")

    # ─── О ПРОГРАММЕ ──────────────────────────────────────────────────────────

    def on_about_clicked(self, *_):
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="Gnome Minecraft Launcher",
            application_icon=APP_ID,
            developer_name="Community",
            version=APP_VERSION,
            comments=(
                "Лаунчер Minecraft для GNOME\n"
                "Поддержка: Microsoft Auth, автоустановка Java, Modrinth"
            ),
            website=f"https://github.com/{GITHUB_REPO}",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present()

    # ─── КОНСОЛЬ ──────────────────────────────────────────────────────────────

    def init_console_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.connect("clicked", lambda *_: self.stack.set_visible_child_name("main"))
        header.pack_start(back_btn)
        lbl = Gtk.Label(label="Консоль / Логи")
        lbl.add_css_class("heading")
        header.set_title_widget(lbl)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.set_tooltip_text("Очистить лог")
        clear_btn.connect("clicked", self.on_clear_console)
        header.pack_end(clear_btn)

        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_tooltip_text("Сохранить лог")
        save_btn.connect("clicked", self.on_save_log)
        header.pack_end(save_btn)
        box.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        self.console_text = Gtk.TextView()
        self.console_text.set_editable(False)
        self.console_text.set_monospace(True)
        self.console_text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.console_buffer = self.console_text.get_buffer()
        scrolled.set_child(self.console_text)
        box.append(scrolled)
        self.stack.add_named(box, "console")

    def append_console(self, text):
        def _do():
            end = self.console_buffer.get_end_iter()
            self.console_buffer.insert(end, text)
            adj = self.console_text.get_parent().get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.idle_add(_do)

    def on_clear_console(self, *_):
        self.console_buffer.set_text("")

    def on_save_log(self, *_):
        text = self.console_buffer.get_text(
            self.console_buffer.get_start_iter(),
            self.console_buffer.get_end_iter(),
            False,
        )
        log_path = os.path.join(self.base_dir, f"launcher-log-{int(time.time())}.txt")
        try:
            with open(log_path, "w") as f:
                f.write(text)
            self.show_toast(f"Лог сохранён: {log_path}")
        except Exception as e:
            self.show_toast(f"Ошибка сохранения лога: {e}")

    # ─── РЕСУРСЫ, ШЕЙДЕРЫ, МИРЫ ──────────────────────────────────────────────

    def init_assets_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.connect("clicked", lambda *_: self.stack.set_visible_child_name("main"))
        header.pack_start(back_btn)
        lbl = Gtk.Label(label="Ресурсы и Миры")
        lbl.add_css_class("heading")
        header.set_title_widget(lbl)
        box.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        target_group = Adw.PreferencesGroup(title="Целевая сборка")
        target_hint = Adw.ActionRow(
            title="Добавлять ресурсы в сборку:",
            subtitle="Файлы будут скопированы в папку выбранной сборки",
        )
        self.assets_instance_model = Gtk.StringList.new(["Vanilla (Default)"])
        self.assets_instance_dropdown = Gtk.DropDown(model=self.assets_instance_model)
        target_hint.add_suffix(self.assets_instance_dropdown)
        target_group.add(target_hint)
        content.append(target_group)

        def _make_asset_group(title, subfolder, pattern, label):
            group = Adw.PreferencesGroup(title=title)
            add_row = Adw.ActionRow(title=f"Добавить {label}")
            add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
            add_btn.add_css_class("suggested-action")
            add_btn.connect("clicked", lambda *_: self._add_asset_to_instance(subfolder, pattern, label))
            add_row.add_suffix(add_btn)
            open_row = Adw.ActionRow(title="Открыть папку")
            open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
            open_btn.add_css_class("flat")
            open_btn.connect("clicked", lambda *_: self._open_asset_folder(subfolder))
            open_row.add_suffix(open_btn)
            group.add(add_row)
            group.add(open_row)
            listbox = Gtk.ListBox()
            listbox.add_css_class("boxed-list")
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            return group, listbox

        rp_group, self.rp_listbox = _make_asset_group(
            "🎨 Ресурспаки", "resourcepacks", "*.zip", "ресурспак (.zip)"
        )
        sh_group, self.sh_listbox = _make_asset_group(
            "✨ Шейдеры", "shaderpacks", "*.zip", "шейдерпак (.zip)"
        )

        content.append(rp_group)
        content.append(self.rp_listbox)
        content.append(sh_group)
        content.append(self.sh_listbox)

        w_group = Adw.PreferencesGroup(title="🌍 Сохранённые миры")
        w_open_row = Adw.ActionRow(title="Открыть папку миров")
        w_open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        w_open_btn.add_css_class("flat")
        w_open_btn.connect("clicked", lambda *_: self._open_asset_folder("saves"))
        w_open_row.add_suffix(w_open_btn)
        w_import_row = Adw.ActionRow(title="Импортировать мир (.zip)")
        w_import_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        w_import_btn.add_css_class("suggested-action")
        w_import_btn.connect("clicked", self.on_import_world)
        w_import_row.add_suffix(w_import_btn)
        w_group.add(w_open_row)
        w_group.add(w_import_row)
        content.append(w_group)

        self.worlds_listbox = Gtk.ListBox()
        self.worlds_listbox.add_css_class("boxed-list")
        self.worlds_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        content.append(self.worlds_listbox)

        ss_group = Adw.PreferencesGroup(title="📸 Скриншоты")
        ss_open = Adw.ActionRow(title="Открыть папку скриншотов")
        ss_open_btn = Gtk.Button(icon_name="camera-photo-symbolic", valign=Gtk.Align.CENTER)
        ss_open_btn.add_css_class("flat")
        ss_open_btn.connect("clicked", lambda *_: self._open_asset_folder("screenshots"))
        ss_open.add_suffix(ss_open_btn)
        ss_group.add(ss_open)
        content.append(ss_group)

        refresh_btn = Gtk.Button(label="🔄 Обновить список")
        refresh_btn.connect("clicked", lambda *_: self.refresh_assets())
        content.append(refresh_btn)

        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(content)
        scrolled.set_child(clamp)
        box.append(scrolled)
        self.stack.add_named(box, "assets")
        self.stack.connect("notify::visible-child", self._on_stack_changed)

    def _get_assets_game_dir(self):
        item = self.assets_instance_dropdown.get_selected_item()
        if not item:
            return self.base_dir
        inst_name = item.get_string()
        return self.base_dir if "Vanilla" in inst_name else os.path.join(self.instances_dir, inst_name)

    def _open_asset_folder(self, subfolder):
        game_dir = self._get_assets_game_dir()
        target = os.path.join(game_dir, subfolder) if subfolder else game_dir
        os.makedirs(target, exist_ok=True)
        open_uri(target)  # FIX: portal-совместимый вызов

    def _sync_assets_instance_dropdown(self):
        dirs = sorted([
            d for d in os.listdir(self.instances_dir)
            if os.path.isdir(os.path.join(self.instances_dir, d))
        ])
        all_names = ["Vanilla (Default)"] + dirs
        self.assets_instance_model.splice(0, self.assets_instance_model.get_n_items(), all_names)

        main_item = self.instance_dropdown.get_selected_item()
        if main_item:
            main_name = main_item.get_string()
            for i, name in enumerate(all_names):
                if name == main_name:
                    self.assets_instance_dropdown.set_selected(i)
                    break

        # FIX: only connect signal once, not every time this method is called
        if not getattr(self, "_assets_dropdown_signal_connected", False):
            self.assets_instance_dropdown.connect(
                "notify::selected",
                lambda *_: self.refresh_assets()
            )
            self._assets_dropdown_signal_connected = True

    def _on_stack_changed(self, stack, _):
        name = stack.get_visible_child_name()
        if name == "assets":
            self._sync_assets_instance_dropdown()
            self.refresh_assets()
        elif name == "modrinth":
            mc_ver    = self._get_current_mc_version() or "?"
            loader    = self._get_current_loader()
            inst      = self.instance_dropdown.get_selected_item()
            inst_name = inst.get_string() if inst else "?"
            self.modrinth_hint.set_text(
                f"Поиск для: MC {mc_ver} · {loader} · Сборка: {inst_name}"
            )

    def refresh_assets(self):
        game_dir = self._get_assets_game_dir()
        self._refresh_dir_list(self.rp_listbox,     os.path.join(game_dir, "resourcepacks"), "resourcepacks")
        self._refresh_dir_list(self.sh_listbox,     os.path.join(game_dir, "shaderpacks"),   "shaderpacks")
        self._refresh_dir_list(self.worlds_listbox, os.path.join(game_dir, "saves"),         "saves", is_worlds=True)

    def _refresh_dir_list(self, listbox, folder, folder_name, is_worlds=False):
        while (child := listbox.get_first_child()):
            listbox.remove(child)
        if not os.path.exists(folder):
            empty_row = Adw.ActionRow(title="Папка пуста")
            empty_row.set_sensitive(False)
            listbox.append(empty_row)
            return
        items = sorted(os.listdir(folder))
        if not items:
            empty_row = Adw.ActionRow(title="Файлов нет")
            empty_row.set_sensitive(False)
            listbox.append(empty_row)
            return
        for name in items:
            row = Adw.ActionRow(title=name)
            if is_worlds:
                icon = "folder-symbolic"
                size_str = self._get_dir_size_str(os.path.join(folder, name))
                row.set_subtitle(size_str)
            else:
                icon = "application-zip-symbolic"
                size_str = self._get_file_size_str(os.path.join(folder, name))
                row.set_subtitle(size_str)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            del_btn.add_css_class("destructive-action")
            del_btn.connect("clicked", self._on_asset_delete, folder, name)
            row.add_suffix(del_btn)
            listbox.append(row)

    def _get_file_size_str(self, path):
        try:
            size = os.path.getsize(path)
            if size > 1024 * 1024:
                return f"{size / (1024*1024):.1f} МБ"
            return f"{size / 1024:.0f} КБ"
        except Exception:
            return ""

    def _get_dir_size_str(self, path):
        try:
            total = 0
            for root, dirs, files in os.walk(path):
                for f in files:
                    total += os.path.getsize(os.path.join(root, f))
            if total > 1024 * 1024:
                return f"{total / (1024*1024):.1f} МБ"
            return f"{total / 1024:.0f} КБ"
        except Exception:
            return ""

    def _on_asset_delete(self, btn, folder, name):
        path = os.path.join(folder, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
            self.refresh_assets()
            self.show_toast(f"Удалено: {name}")
        except Exception as e:
            self.show_toast(f"Ошибка удаления: {e}")

    def _add_asset_to_instance(self, subfolder, pattern, label):
        dialog = Gtk.FileDialog(title=f"Добавить {label}")
        flt = Gtk.FileFilter()
        flt.set_name(label)
        flt.add_pattern(pattern)
        flt.add_pattern("*.zip")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)

        def on_done(d, result):
            try:
                files = d.open_multiple_finish(result)
                game_dir = self._get_assets_game_dir()
                target_dir = os.path.join(game_dir, subfolder)
                os.makedirs(target_dir, exist_ok=True)
                count = 0
                for i in range(files.get_n_items()):
                    f = files.get_item(i)
                    src = f.get_path()
                    dst = os.path.join(target_dir, os.path.basename(src))
                    shutil.copy2(src, dst)
                    count += 1
                inst_item = self.assets_instance_dropdown.get_selected_item()
                inst_name = inst_item.get_string() if inst_item else "?"
                self.refresh_assets()
                self.show_toast(f"✓ Добавлено {count} файл(ов) в «{inst_name}»")
            except GLib.Error:
                pass
            except Exception as e:
                self.show_toast(f"Ошибка: {e}")

        dialog.open_multiple(self, None, on_done)

    def _add_asset(self, subfolder, pattern, label):
        self._add_asset_to_instance(subfolder, pattern, label)

    def on_import_world(self, *_):
        dialog = Gtk.FileDialog(title="Импортировать мир (.zip)")
        flt = Gtk.FileFilter()
        flt.set_name("Архив мира (*.zip)")
        flt.add_pattern("*.zip")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)

        def on_done(d, result):
            try:
                f = d.open_finish(result)
                zip_path = f.get_path()
                game_dir = self._get_assets_game_dir()
                saves_dir = os.path.join(game_dir, "saves")
                os.makedirs(saves_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path) as z:
                    # FIX: check for path traversal before extracting
                    for member in z.namelist():
                        member_path = os.path.realpath(os.path.join(saves_dir, member))
                        if not member_path.startswith(os.path.realpath(saves_dir)):
                            raise Exception(f"Небезопасный путь в архиве: {member}")
                    z.extractall(saves_dir)
                self.refresh_assets()
                self.show_toast("✓ Мир импортирован")
            except GLib.Error:
                pass
            except Exception as e:
                self.show_toast(f"Ошибка импорта: {e}")

        dialog.open(self, None, on_done)

    def open_folder(self, suffix):
        """Открывает папку текущей сборки (из настроек)."""
        game_dir = self._get_current_game_dir()
        target = os.path.join(game_dir, suffix) if suffix else game_dir
        os.makedirs(target, exist_ok=True)
        open_uri(target)  # FIX: portal-совместимый вызов

    # ─── MODRINTH ─────────────────────────────────────────────────────────────

    def init_modrinth_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.connect("clicked", lambda *_: self.stack.set_visible_child_name("main"))
        header.pack_start(back_btn)
        lbl = Gtk.Label(label="Поиск модов — Modrinth")
        lbl.add_css_class("heading")
        header.set_title_widget(lbl)
        box.append(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        search_box = Gtk.Box(spacing=8)
        self.modrinth_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Название мода...")
        self.modrinth_entry.connect("activate", self.on_modrinth_search)
        search_go = Gtk.Button(label="Найти")
        search_go.add_css_class("suggested-action")
        search_go.connect("clicked", self.on_modrinth_search)
        search_box.append(self.modrinth_entry)
        search_box.append(search_go)
        content.append(search_box)

        self.modrinth_hint = Gtk.Label(opacity=0.5)
        self.modrinth_hint.set_wrap(True)
        content.append(self.modrinth_hint)

        self.modrinth_status = Gtk.Label(
            label="Введите название мода и нажмите Найти", opacity=0.6
        )
        self.modrinth_status.set_wrap(True)
        content.append(self.modrinth_status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        self.modrinth_listbox = Gtk.ListBox()
        self.modrinth_listbox.add_css_class("boxed-list")
        self.modrinth_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.set_child(self.modrinth_listbox)
        content.append(scrolled)

        clamp = Adw.Clamp(maximum_size=620)
        clamp.set_child(content)
        scrolled2 = Gtk.ScrolledWindow()
        scrolled2.set_vexpand(True)
        scrolled2.set_child(clamp)
        box.append(scrolled2)
        self.stack.add_named(box, "modrinth")

    def on_modrinth_search(self, *_):
        query = self.modrinth_entry.get_text().strip()
        if not query:
            return
        mc_ver = self._get_current_mc_version()
        loader = self._get_current_loader()
        while (child := self.modrinth_listbox.get_first_child()):
            self.modrinth_listbox.remove(child)
        self.modrinth_status.set_text("Поиск...")

        def fetch():
            try:
                results = modrinth_search(query, mc_ver, loader)
                GLib.idle_add(self._populate_modrinth_results, results)
            except Exception as e:
                GLib.idle_add(self.modrinth_status.set_text, f"Ошибка поиска: {e}")
                self.show_toast(f"Modrinth недоступен: {str(e)[:60]}")

        threading.Thread(target=fetch, daemon=True).start()

    def _populate_modrinth_results(self, results):
        while (child := self.modrinth_listbox.get_first_child()):
            self.modrinth_listbox.remove(child)
        if not results:
            self.modrinth_status.set_text("Ничего не найдено")
            return
        self.modrinth_status.set_text(f"Найдено: {len(results)}")
        for mod in results:
            row = Adw.ActionRow(
                title=mod.get("title", "?"),
                subtitle=mod.get("description", "")[:100],
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("application-x-addon-symbolic"))
            dl_btn = Gtk.Button(label="Скачать", valign=Gtk.Align.CENTER)
            dl_btn.add_css_class("suggested-action")
            dl_btn.connect("clicked", self.on_modrinth_download_clicked, mod)
            row.add_suffix(dl_btn)
            self.modrinth_listbox.append(row)

    def on_modrinth_download_clicked(self, btn, mod):
        def do_download(inst_name, game_dir):
            btn.set_sensitive(False)
            btn.set_label("...")
            mc_ver = self._get_current_mc_version()
            loader = self._get_current_loader()
            project_id = mod["project_id"]
            mods_dir = os.path.join(game_dir, "mods")
            os.makedirs(mods_dir, exist_ok=True)

            def fetch():
                try:
                    versions = modrinth_get_versions(project_id, mc_ver, loader)
                    if not versions:
                        self.show_toast("Нет подходящей версии для этого MC/загрузчика")
                        GLib.idle_add(btn.set_sensitive, True)
                        GLib.idle_add(btn.set_label, "Скачать")
                        return

                    version_data = versions[0]
                    file_info = version_data["files"][0]
                    dest = os.path.join(mods_dir, file_info["filename"])
                    self.show_toast(f"Скачиваю {file_info['filename']}...")

                    download_file(file_info["url"], dest)

                    deps = modrinth_resolve_dependencies(version_data, mc_ver, loader)
                    downloaded_deps = 0
                    for dep in deps:
                        dep_dest = os.path.join(mods_dir, dep["filename"])
                        if not os.path.exists(dep_dest):
                            try:
                                download_file(dep["url"], dep_dest)
                                downloaded_deps += 1
                            except Exception as e:
                                print(f"[DEPS] {dep['filename']}: {e}")
                                self.show_toast(f"Зависимость {dep['filename']}: ошибка")

                    GLib.idle_add(self.refresh_mods_list)
                    summary = f"✓ {file_info['filename']}"
                    if downloaded_deps:
                        summary += f" (+{downloaded_deps} зависимостей)"
                    self.show_toast(summary)
                    GLib.idle_add(btn.set_label, "✓")

                except Exception as e:
                    self.show_toast(f"Ошибка скачивания: {str(e)[:80]}")
                    GLib.idle_add(btn.set_sensitive, True)
                    GLib.idle_add(btn.set_label, "Скачать")

            threading.Thread(target=fetch, daemon=True).start()

        self._show_instance_picker(do_download)

    # ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────

    def init_settings_page(self):
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.connect("clicked", lambda *_: self.stack.set_visible_child_name("main"))
        header.pack_start(back_btn)
        lbl = Gtk.Label(label="Настройки")
        lbl.add_css_class("heading")
        header.set_title_widget(lbl)
        settings_box.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        settings_box.append(scrolled)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        # Java
        java_group = Adw.PreferencesGroup(title="Java")
        self.java_entry = Adw.EntryRow(title="Путь к java (пусто = авто)")
        java_entry_icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        java_entry_icon.set_pixel_size(16)
        self.java_entry.add_prefix(java_entry_icon)

        hint_row = Adw.ActionRow(
            title="Автоматическое управление Java",
            subtitle="MC &lt; 1.17 → Java 8  ·  1.17 → Java 16  ·  1.18–1.20 → Java 17  ·  1.20.5+ → Java 21",
        )
        hint_row.set_sensitive(False)

        dl_java_row = Adw.ActionRow(
            title="Скачать Java автоматически",
            subtitle="Загружает нужную версию JRE в папку лаунчера (Eclipse Temurin / Adoptium)",
        )
        dl_java_icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        dl_java_icon.set_pixel_size(16)
        dl_java_row.add_prefix(dl_java_icon)

        dl_java_btn = Gtk.Button(label="Скачать", valign=Gtk.Align.CENTER)
        dl_java_btn.add_css_class("suggested-action")
        dl_java_btn.connect("clicked", self.on_download_java_clicked)
        dl_java_row.add_suffix(dl_java_btn)

        java_group.add(self.java_entry)
        java_group.add(hint_row)
        java_group.add(dl_java_row)
        content.append(java_group)

        # ОЗУ
        ram_group = Adw.PreferencesGroup(title="Ресурсы")
        self.ram_spin = Gtk.SpinButton.new_with_range(1, 64, 1)
        ram_row = Adw.ActionRow(title="ОЗУ (ГБ)")
        ram_row.add_suffix(self.ram_spin)
        ram_group.add(ram_row)
        content.append(ram_group)

        # Фильтры версий
        type_group = Adw.PreferencesGroup(title="Фильтры версий")
        self.show_alpha    = Gtk.Switch(active=True,  valign=Gtk.Align.CENTER)
        self.show_snap     = Gtk.Switch(active=False, valign=Gtk.Align.CENTER)
        self.show_optifine = Gtk.Switch(active=True,  valign=Gtk.Align.CENTER)
        for sw in [self.show_alpha, self.show_snap, self.show_optifine]:
            sw.connect("state-set", lambda *_: GLib.timeout_add(100, self.load_versions))
        row_a = Adw.ActionRow(title="Alpha/Beta");    row_a.add_suffix(self.show_alpha)
        row_s = Adw.ActionRow(title="Снапшоты");      row_s.add_suffix(self.show_snap)
        row_o = Adw.ActionRow(
            title="OptiFine",
            subtitle="Версии с OptiFine (браузер откроется для скачивания)",
        )
        row_o.add_suffix(self.show_optifine)
        type_group.add(row_a)
        type_group.add(row_s)
        type_group.add(row_o)
        content.append(type_group)

        # Папки
        folders_group = Adw.PreferencesGroup(title="Папки")
        for name, suffix in [
            ("Корневая папка",  ""),
            ("Папка модов",     "mods"),
            ("Шейдеры",         "shaderpacks"),
            ("Ресурспаки",      "resourcepacks"),
            ("Скриншоты",       "screenshots"),
            ("Миры",            "saves"),
        ]:
            row = Adw.ActionRow(title=name)
            btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda x, s=suffix: self.open_folder(s))
            row.add_suffix(btn)
            folders_group.add(row)
        content.append(folders_group)

        # Безопасность
        sec_group = Adw.PreferencesGroup(title="Безопасность")
        keyring_ok = _keyring_available()
        sec_row = Adw.ActionRow(
            title="Хранилище токенов",
            subtitle=(
                "✓ keyring доступен — токен MS хранится безопасно"
                if keyring_ok else
                "⚠ keyring не найден — токен хранится в файле. "
                "Установите python3-keyring для защиты."
            ),
        )
        sec_group.add(sec_row)
        content.append(sec_group)

        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(content)
        scrolled.set_child(clamp)
        self.stack.add_named(settings_box, "settings")

    def on_download_java_clicked(self, btn):
        item = self.version_dropdown.get_selected_item() if hasattr(self, "version_dropdown") else None
        mc_ver = item.get_string().replace(" + OptiFine", "") if item else "1.20"
        java_ver = get_required_java_version(mc_ver)
        btn.set_sensitive(False)
        btn.set_label("Скачиваю...")

        def do_dl():
            try:
                path = download_jre(
                    self.java_dir, java_ver,
                    status_cb=lambda s: GLib.idle_add(self.status_label.set_text, s),
                    # FIX: progress_cb передаём напрямую (0.0–1.0), без умножения
                    progress_cb=lambda p: GLib.idle_add(self.progress_bar.set_fraction, float(p)),
                )
                self.show_toast(f"✓ Java {java_ver} установлена: {path}")
                GLib.idle_add(self._on_version_changed)
            except Exception as e:
                self.show_toast(f"Ошибка скачивания Java: {str(e)[:80]}")
            GLib.idle_add(btn.set_sensitive, True)
            GLib.idle_add(btn.set_label, "Скачать")

        self.progress_bar.set_visible(True)
        threading.Thread(target=do_dl, daemon=True).start()

    # ─── СБОРКИ ───────────────────────────────────────────────────────────────

    def _get_current_mc_version(self):
        item = self.version_dropdown.get_selected_item()
        return item.get_string() if item else None

    def _get_current_loader(self):
        idx = self.loader_choice.get_selected()
        return ["Vanilla", "Fabric", "Forge", "Quilt", "NeoForge"][idx]

    def _get_current_game_dir(self):
        item = self.instance_dropdown.get_selected_item()
        if not item:
            return self.base_dir
        inst_name = item.get_string()
        return self.base_dir if "Vanilla" in inst_name else os.path.join(self.instances_dir, inst_name)

    def refresh_mods_list(self):
        while (child := self.mods_listbox.get_first_child()):
            self.mods_listbox.remove(child)
        game_dir = self._get_current_game_dir()
        mods_dir = os.path.join(game_dir, "mods")
        mods = (
            [f for f in os.listdir(mods_dir) if f.endswith(".jar")]
            if os.path.exists(mods_dir) else []
        )
        if not mods:
            placeholder = Adw.ActionRow(
                title="Модов нет",
                subtitle="Нажмите 🔍 для поиска на Modrinth или 📁 для добавления .jar",
            )
            placeholder.add_prefix(Gtk.Image.new_from_icon_name("application-x-addon-symbolic"))
            self.mods_listbox.append(placeholder)
            return
        mc_ver = self._get_current_mc_version()
        loader = self._get_current_loader()
        for mod_file in sorted(mods):
            jar_path = os.path.join(mods_dir, mod_file)
            ok, reason = check_mod_compatibility(jar_path, mc_ver, loader)
            row = Adw.ActionRow(title=mod_file)
            row.add_prefix(Gtk.Image.new_from_icon_name("application-x-addon-symbolic"))
            if not ok:
                row.set_subtitle(f"⚠ {reason}")
                row.add_css_class("warning")
            del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            del_btn.add_css_class("destructive-action")
            del_btn.connect("clicked", self.on_delete_mod_clicked, mods_dir, mod_file)
            row.add_suffix(del_btn)
            self.mods_listbox.append(row)

    def on_delete_mod_clicked(self, btn, mods_dir, mod_file):
        path = os.path.join(mods_dir, mod_file)
        try:
            if os.path.exists(path):
                os.remove(path)
            self.refresh_mods_list()
            self.show_toast("Мод удалён")
        except Exception as e:
            self.show_toast(f"Ошибка: {e}")

    def on_add_mod_clicked(self, btn):
        dialog = Gtk.FileDialog(title="Добавить моды")
        jar_filter = Gtk.FileFilter()
        jar_filter.set_name("Minecraft Mods (*.jar)")
        jar_filter.add_pattern("*.jar")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(jar_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(jar_filter)

        def on_done(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
                game_dir = self._get_current_game_dir()
                target_dir = os.path.join(game_dir, "mods")
                os.makedirs(target_dir, exist_ok=True)
                count = 0
                for i in range(files.get_n_items()):
                    f = files.get_item(i)
                    shutil.copy2(
                        f.get_path(),
                        os.path.join(target_dir, os.path.basename(f.get_path())),
                    )
                    count += 1
                self.refresh_mods_list()
                self.show_toast(f"Добавлено модов: {count}")
            except GLib.Error:
                pass

        dialog.open_multiple(self, None, on_done)

    def _show_instance_picker(self, callback):
        dialog = Adw.MessageDialog(transient_for=self, heading="В какую сборку?")
        model = Gtk.StringList.new(["Vanilla (Default)"])
        dirs = sorted([
            d for d in os.listdir(self.instances_dir)
            if os.path.isdir(os.path.join(self.instances_dir, d))
        ])
        for d in dirs:
            model.append(d)
        dropdown = Gtk.DropDown(model=model)
        dropdown.set_selected(self.instance_dropdown.get_selected())
        dialog.set_extra_child(dropdown)
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("ok", "Добавить")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_res(d, r):
            if r == "ok":
                item = dropdown.get_selected_item()
                if item:
                    inst_name = item.get_string()
                    game_dir = (
                        self.base_dir if "Vanilla" in inst_name
                        else os.path.join(self.instances_dir, inst_name)
                    )
                    callback(inst_name, game_dir)
            d.destroy()

        dialog.connect("response", on_res)
        dialog.present()

    def on_create_instance_clicked(self, btn):
        dialog = Adw.MessageDialog(transient_for=self, heading="Новая сборка")
        entry = Gtk.Entry(placeholder_text="Имя сборки...")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("create", "Создать")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        def on_res(d, r):
            name = entry.get_text().strip()
            if r == "create" and name:
                for subfolder in ["mods", "resourcepacks", "shaderpacks", "saves", "screenshots"]:
                    os.makedirs(os.path.join(self.instances_dir, name, subfolder), exist_ok=True)
                self.refresh_instances()
                self.show_toast(f"Сборка «{name}» создана")
            d.destroy()

        dialog.connect("response", on_res)
        dialog.present()

    def on_delete_instance_clicked(self, btn):
        idx = self.instance_dropdown.get_selected()
        if idx == 0:
            self.show_toast("Нельзя удалить Vanilla (Default)")
            return
        name = self.instance_dropdown.get_selected_item().get_string()
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Удалить «{name}»?",
            body="Все файлы сборки и моды будут удалены безвозвратно.",
        )
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("delete", "Удалить")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_res(d, r):
            if r == "delete":
                shutil.rmtree(os.path.join(self.instances_dir, name), ignore_errors=True)
                self.refresh_instances()
                self.show_toast(f"Сборка «{name}» удалена")
            d.destroy()

        dialog.connect("response", on_res)
        dialog.present()

    def refresh_instances(self):
        dirs = sorted([
            d for d in os.listdir(self.instances_dir)
            if os.path.isdir(os.path.join(self.instances_dir, d))
        ])
        all_names = ["Vanilla (Default)"] + dirs
        self.instance_model.splice(1, self.instance_model.get_n_items() - 1, dirs)

        last_inst = getattr(self, "_last_instance", None) or self.config["last_instance"]
        selected_idx = 0
        if last_inst:
            for i, name in enumerate(all_names):
                if name == last_inst:
                    selected_idx = i
                    break
        self.instance_dropdown.set_selected(selected_idx)
        self._last_instance = None

        self.refresh_mods_list()

    # ─── ВЕРСИИ ───────────────────────────────────────────────────────────────

    def load_versions(self):
        global _versions_cache, _versions_cache_time
        GLib.idle_add(self.status_label.set_text, "Загрузка версий...")

        def fetch():
            global _versions_cache, _versions_cache_time
            try:
                now = time.time()
                if _versions_cache and (now - _versions_cache_time) < VERSIONS_CACHE_TTL:
                    all_v = _versions_cache
                else:
                    all_v = minecraft_launcher_lib.utils.get_version_list()
                    _versions_cache = all_v
                    _versions_cache_time = now

                filtered = []
                for v in all_v:
                    vtype = v["type"]
                    if vtype == "release":
                        filtered.append(v["id"])
                    elif vtype in ["old_alpha", "old_beta"] and self.show_alpha.get_active():
                        filtered.append(v["id"])
                    elif vtype == "snapshot" and self.show_snap.get_active():
                        filtered.append(v["id"])
                optifine_versions = []
                if self.show_optifine.get_active():
                    for mc_ver in get_optifine_versions():
                        optifine_versions.append(f"{mc_ver} + OptiFine")
                GLib.idle_add(self.update_ui_versions, filtered, optifine_versions)
            except Exception as e:
                GLib.idle_add(self.status_label.set_text, f"Ошибка загрузки версий: {e}")
                self.show_toast(f"Не удалось загрузить список версий: {str(e)[:60]}")

        threading.Thread(target=fetch, daemon=True).start()

    def update_ui_versions(self, versions, optifine_versions=None):
        # Block the signal while rebuilding to avoid _on_version_changed firing
        # with item=None (which happens when INVALID_LIST_POSITION is set)
        self.version_dropdown.handler_block_by_func(self._on_version_changed)
        try:
            all_versions = list(optifine_versions or []) + list(versions)
            self.version_model.splice(0, self.version_model.get_n_items(), all_versions)
            saved = getattr(self, "_last_version", None)
            if saved:
                for i, v in enumerate(all_versions):
                    if v == saved:
                        self.version_dropdown.set_selected(i)
                        break
                else:
                    self.version_dropdown.set_selected(0)
            else:
                self.version_dropdown.set_selected(0)
        finally:
            self.version_dropdown.handler_unblock_by_func(self._on_version_changed)
        loader_idx = getattr(self, "_last_loader", None)
        self.loader_choice.set_selected(loader_idx if loader_idx is not None else 0)
        self.status_label.set_text("Готов")
        self.refresh_mods_list()
        # Fire once manually after unblock
        self._on_version_changed()

    # ─── ЗАПУСК ───────────────────────────────────────────────────────────────

    def on_launch_clicked(self, button):
        item = self.version_dropdown.get_selected_item()
        if not item:
            self.show_toast("Версия не выбрана")
            return
        self.save_launcher_config()

        ver_str  = item.get_string()
        game_dir = self._get_current_game_dir()
        loader   = self._get_current_loader()
        ram      = int(self.ram_spin.get_value())
        java_path_custom = self.java_entry.get_text().strip()

        is_optifine = " + OptiFine" in ver_str
        mc_version  = ver_str.replace(" + OptiFine", "") if is_optifine else ver_str

        if self.ms_account:
            username = self.ms_account["username"]
            uuid     = self.ms_account["uuid"]
            token    = self.ms_account.get("access_token", "0")
        else:
            username = self.account_entry.get_text().strip() or "Player"
            uuid     = "0"
            token    = "0"

        self.launch_btn.set_sensitive(False)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.append_console(
            f"\n{'='*50}\n[ЗАПУСК] {ver_str} | {loader} | {ram}G RAM | {username}\n{'='*50}\n"
        )

        self.get_application().hold()
        self._game_running = True

        threading.Thread(
            target=self.run_engine,
            args=(username, uuid, token, mc_version, loader, ram, game_dir, java_path_custom, is_optifine),
            daemon=True,
        ).start()

    def run_engine(self, username, uuid, token, version_id, loader, ram,
                   game_dir, java_path_custom, install_optifine=False):

        def status(msg):
            GLib.idle_add(self.status_label.set_text, msg)
            self.append_console(f"[STATUS] {msg}\n")

        # FIX: minecraft_launcher_lib передаёт прогресс в диапазоне 0–max (целые числа),
        # поэтому нормируем через setMax
        _progress_max = [100]

        def set_max(m):
            if m and m > 0:
                _progress_max[0] = m

        def progress(val):
            GLib.idle_add(self.progress_bar.set_fraction, float(val) / _progress_max[0])

        cb = {"setStatus": status, "setProgress": progress, "setMax": set_max}

        try:
            target_version = version_id
            status(f"Установка {version_id}...")
            minecraft_launcher_lib.install.install_minecraft_version(
                version_id, self.base_dir, callback=cb
            )

            # ── OptiFine ────────────────────────────────────────────────────
            if install_optifine:
                status("Установка OptiFine...")
                of_url, of_filename = get_optifine_download_url(version_id)
                if of_filename:
                    mods_dir = os.path.join(game_dir, "mods")
                    os.makedirs(mods_dir, exist_ok=True)
                    of_dest = os.path.join(mods_dir, of_filename)
                    if not os.path.exists(of_dest):
                        status("OptiFine: открываю браузер для скачивания...")
                        open_uri(f"https://optifine.net/adloadx?f={of_filename}")  # FIX: portal
                        self.show_toast("Скачайте OptiFine вручную и добавьте в папку mods")
                    else:
                        status("OptiFine уже установлен")

            # ── Загрузчики ──────────────────────────────────────────────────
            if loader == "Fabric":
                status("Установка Fabric...")
                minecraft_launcher_lib.fabric.install_fabric(version_id, self.base_dir, callback=cb)
                installed = minecraft_launcher_lib.utils.get_installed_versions(self.base_dir)
                matches = [v["id"] for v in installed
                           if "fabric" in v["id"].lower() and version_id in v["id"]]
                if matches:
                    target_version = matches[-1]

            elif loader == "Forge":
                status("Поиск Forge...")
                forge_v = minecraft_launcher_lib.forge.find_forge_version(version_id)
                if not forge_v:
                    raise Exception(f"Forge не найден для {version_id}")
                status(f"Установка Forge {forge_v}...")
                minecraft_launcher_lib.forge.install_forge_version(forge_v, self.base_dir, callback=cb)
                installed = minecraft_launcher_lib.utils.get_installed_versions(self.base_dir)
                forge_suffix = forge_v.split("-")[-1]
                target_version = next(
                    (v["id"] for v in installed
                     if version_id in v["id"] and forge_suffix in v["id"]),
                    f"{version_id}-forge-{forge_suffix}",
                )

            elif loader == "Quilt":
                status("Установка Quilt...")
                minecraft_launcher_lib.quilt.install_quilt(version_id, self.base_dir, callback=cb)
                installed = minecraft_launcher_lib.utils.get_installed_versions(self.base_dir)
                matches = [v["id"] for v in installed
                           if "quilt" in v["id"].lower() and version_id in v["id"]]
                if matches:
                    target_version = matches[-1]

            elif loader == "NeoForge":
                status("Установка NeoForge...")
                try:
                    neoforge_v = minecraft_launcher_lib.forge.find_forge_version(version_id)
                    if neoforge_v:
                        minecraft_launcher_lib.forge.install_forge_version(
                            neoforge_v, self.base_dir, callback=cb
                        )
                        installed = minecraft_launcher_lib.utils.get_installed_versions(self.base_dir)
                        matches = [v["id"] for v in installed
                                   if "neoforge" in v["id"].lower() and version_id in v["id"]]
                        if not matches:
                            matches = [v["id"] for v in installed
                                       if "forge" in v["id"].lower() and version_id in v["id"]]
                        if matches:
                            target_version = matches[-1]
                except Exception as nf_err:
                    self.append_console(f"[NeoForge] {nf_err}\n")
                    self.show_toast("NeoForge: используем Forge-метод")

            # ── Java ────────────────────────────────────────────────────────
            status(f"Поиск Java для {version_id}...")
            java_exec = get_or_download_java(
                self.java_dir, version_id, java_path_custom,
                status_cb=status,
                # FIX: download_file отдаёт 0.0–1.0, передаём напрямую
                progress_cb=lambda p: GLib.idle_add(self.progress_bar.set_fraction, float(p)),
            )
            self.append_console(f"[JAVA] {java_exec}\n")

            options = {
                "username":       username,
                "uuid":           uuid,
                "token":          token,
                "jvmArguments":   [f"-Xmx{ram}G", f"-Xms{ram}G"],
                "gameDirectory":  game_dir,
                "executablePath": java_exec,
            }

            command = minecraft_launcher_lib.command.get_minecraft_command(
                target_version, self.base_dir, options
            )
            self.append_console(f"[CMD] {' '.join(command[:6])} ...\n")
            status("Игра запущена")
            self.show_toast("✓ Игра запущена!")

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            for line in process.stdout:
                self.append_console(line)

            process.wait()
            exit_code = process.returncode
            self.append_console(f"\n[ИГРА ЗАВЕРШЕНА] Код выхода: {exit_code}\n")

            if exit_code == 0:
                self.show_toast("Игра завершена")
            else:
                self.show_toast(f"Игра завершена с ошибкой (код {exit_code})")

        except Exception as e:
            err = str(e)
            self.append_console(f"\n[ОШИБКА] {err}\n")
            self.show_toast(f"Ошибка: {err[:80]}")
            GLib.idle_add(self.status_label.set_text, f"Ошибка: {err[:50]}")

        finally:
            self._game_running = False
            GLib.idle_add(self._on_game_finished)

    def _on_game_finished(self):
        self.launch_btn.set_sensitive(True)
        self.progress_bar.set_visible(False)
        self.status_label.set_text("Готов")
        self.get_application().release()

        last_log = self.console_buffer.get_text(
            self.console_buffer.get_start_iter(),
            self.console_buffer.get_end_iter(),
            False,
        )
        if "[ОШИБКА]" in last_log[-500:]:
            self.stack.set_visible_child_name("console")

        return False


# ─── ПРИЛОЖЕНИЕ ───────────────────────────────────────────────────────────────

class App(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = MainWindow(application=self)
        win.present()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()

import minecraft_launcher_lib
import subprocess
import sys

class MinecraftLauncher:
    def __init__(self, game_dir):
        self.game_dir = game_dir

    def install_and_start(self, versionid, username):
        # Скачиваем всё необходимое
        minecraft_launcher_lib.install.install_minecraft_version(versionid, self.game_dir)

        # FIX: gameDirectory must be passed so saves/mods/resourcepacks
        # resolve to the right instance folder, not the launcher root.
        options = {
            "username": username,
            "uuid": "0",
            "token": "0",
            "gameDirectory": self.game_dir,
        }

        # Получаем команду запуска
        command = minecraft_launcher_lib.command.get_minecraft_command(
            versionid, self.game_dir, options
        )

        # FIX: use Popen instead of call so the launcher UI stays responsive.
        # Also capture returncode so callers can detect crashes.
        try:
            process = subprocess.Popen(command)
            return process
        except FileNotFoundError as e:
            raise RuntimeError(f"Java не найдена: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Ошибка запуска игры: {e}") from e
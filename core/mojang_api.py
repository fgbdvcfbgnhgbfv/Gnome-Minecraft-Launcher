import minecraft_launcher_lib

class MojangAPI:
    @staticmethod
    def get_all_versions():
        # Получаем вообще все версии, которые знает библиотека
        return minecraft_launcher_lib.utils.get_version_list()

    @staticmethod
    def get_fabric_versions(mc_version):
        # Простая проверка доступности Fabric для конкретной версии
        try:
            return minecraft_launcher_lib.fabric.get_stable_minecraft_versions()
        except:
            return []
import os
import struct
from krita import Extension, Krita

try:
    from PyQt6.QtCore import QTimer
except ImportError:
    from PyQt5.QtCore import QTimer


def _read_u32(f):
    return struct.unpack(">I", f.read(4))[0]


def _read_i16(f):
    return struct.unpack(">h", f.read(2))[0]


def get_psd_folder_states(filepath):
    """
    Читает .psd файл и возвращает словарь:
    { ((0, "Папка"), (1, "Подпапка")): True/False },
    где True = папка закрыта (collapsed).
    Порядковый индекс папки на уровне защищает от коллизий при одинаковых именах.
    """
    records = []

    with open(filepath, "rb") as f:
        if f.read(4) != b"8BPS":
            return {}
        f.seek(26)

        # Пропуск секций Color Mode Data и Image Resources
        f.seek(_read_u32(f), 1)
        f.seek(_read_u32(f), 1)

        # Секция Layer and Mask Information
        layer_mask_len = _read_u32(f)
        if layer_mask_len == 0:
            return {}

        layer_info_len = _read_u32(f)
        layer_count = abs(_read_i16(f))

        for _ in range(layer_count):
            f.seek(16, 1)  # Координаты слоя
            num_channels = struct.unpack(">H", f.read(2))[0]
            f.seek(num_channels * 6, 1)
            f.seek(12, 1)  # Режим наложения, opacity, flags

            extra_len = _read_u32(f)
            extra_end = f.tell() + extra_len

            mask_len = _read_u32(f)
            f.seek(mask_len, 1)

            ranges_len = _read_u32(f)
            f.seek(ranges_len, 1)

            name_len = f.read(1)[0]
            raw_name = f.read(name_len)
            padded_len = (name_len + 1 + 3) & ~3
            f.seek(padded_len - (name_len + 1), 1)

            unicode_name = None
            section_type = 0

            while f.tell() < extra_end:
                sig = f.read(4)
                key = f.read(4)
                block_len = _read_u32(f)
                block_start = f.tell()

                if key in (b"lsct", b"lsdk") and block_len >= 4:
                    section_type = _read_u32(f)
                elif key == b"luni" and block_len >= 4:
                    char_count = _read_u32(f)
                    unicode_name = f.read(char_count * 2).decode("utf-16-be", errors="replace")

                padded_block = (block_len + 1) & ~1
                f.seek(block_start + padded_block)

            f.seek(extra_end)

            final_name = unicode_name if unicode_name is not None else raw_name.decode("cp1251", errors="replace")
            records.append((final_name, section_type))

    folder_states = {}
    stack = []              # Стек элементов вида (folder_index, folder_name)
    level_counters = [0]    # Счётчик папок на каждом уровне вложенности

    for name, stype in reversed(records):
        if stype in (1, 2):
            idx = level_counters[-1]
            level_counters[-1] += 1

            step = (idx, name)
            current_path = tuple(stack + [step])
            folder_states[current_path] = (stype == 2)

            stack.append(step)
            level_counters.append(0)    # Новый счётчик для вложенных папок внутри текущей
        elif stype == 3:
            if stack:
                stack.pop()
                level_counters.pop()

    return folder_states


class PsdFolderStatesExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self._pending_paths = []

    def setup(self):
        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.imageCreated.connect(self._on_image_created)

    def createActions(self, window):
        pass

    def _on_image_created(self, image):
        if image is None:
            return

        filepath = image.fileName()
        if not filepath or not filepath.lower().endswith(".psd"):
            return

        if not os.path.isfile(filepath):
            return

        self._pending_paths.append(filepath)
        QTimer.singleShot(250, self._process_pending)

    def _process_pending(self):
        if not self._pending_paths:
            return

        filepath = self._pending_paths.pop(0)
        self._apply_states(filepath)

    def _apply_states(self, filepath):
        target_doc = None
        for doc in Krita.instance().documents():
            if doc.fileName() == filepath:
                target_doc = doc
                break

        if target_doc is None:
            return

        try:
            folder_states = get_psd_folder_states(filepath)
        except Exception as e:
            print(f"[PSD Folder States] Ошибка чтения PSD: {e}")
            return

        if not folder_states:
            return

        root = target_doc.rootNode()
        if root is None:
            return

        self._walk_and_apply(root, (), folder_states)

    def _walk_and_apply(self, parent_node, current_path, folder_states):
        folder_idx = 0
        for node in reversed(parent_node.childNodes()):
            if node.type() == "grouplayer":
                step = (folder_idx, node.name())
                folder_idx += 1

                node_path = current_path + (step,)
                self._walk_and_apply(node, node_path, folder_states)

                if node_path in folder_states:
                    node.setCollapsed(folder_states[node_path])
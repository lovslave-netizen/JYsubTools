"""JYPlayer - 단축키 중심 개인용 동영상 플레이어 (PySide6 + libmpv)

사용법: jyplayer.py [폴더 또는 동영상 파일]
"""
import ctypes
import json
import os
import random
import re
import sys
import threading
from ctypes import wintypes


def resource_dir():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# libmpv-2.dll 을 찾을 수 있도록 PATH에 추가한 뒤 mpv import
# 설치형: 설치폴더\bin (공용) 도 함께 찾는다
for _d in (resource_dir(), os.path.normpath(os.path.join(app_dir(), "..", "bin"))):
    if os.path.isdir(_d):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_d)

_COMMON = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "common")
if os.path.isdir(_COMMON):  # 개발 중(스크립트 실행)에는 repo 의 common 폴더를 사용
    sys.path.insert(0, _COMMON)
import jyupdate  # noqa: E402
import mpv  # noqa: E402
from send2trash import send2trash  # noqa: E402
from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QSlider, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".rm", ".rmvb", ".asf", ".divx",
}
SETTINGS_PATH = os.path.join(app_dir(), "JYPlayer_settings.json")

# Windows 가상키 코드 - 키보드 배열/한글 입력 상태와 무관하게 동작시키기 위해 사용
VK_SHIFT, VK_CONTROL, VK_HANJA = 0x10, 0x11, 0x19
VK_ESCAPE, VK_RETURN, VK_SPACE, VK_PRIOR, VK_NEXT = 0x1B, 0x0D, 0x20, 0x21, 0x22
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_DELETE = 0x25, 0x26, 0x27, 0x28, 0x2E
VK_F, VK_P, VK_R = 0x46, 0x50, 0x52
VK_C, VK_X = 0x43, 0x58
VK_MULTIPLY, VK_ADD, VK_SUBTRACT = 0x6A, 0x6B, 0x6D  # 숫자패드 * + -
ZOOM_STEP = 0.1  # mpv video-zoom 은 로그(2^x) 단위
OSD_FONT_SIZE = 22  # 화면에 뜨는 안내 글자 크기 (720p 기준)

# (Ctrl, Shift) -> 이동 초
SEEK_STEPS = {(False, False): 5, (False, True): 10, (True, False): 30, (True, True): 60}
VOLUME_STEP = 5
VOLUME_MAX = 130

user32 = ctypes.windll.user32
user32.GetKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.restype = ctypes.c_short
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def key_down(vk):
    return bool(user32.GetKeyState(vk) & 0x8000)


def async_key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def disable_child_windows(hwnd):
    """mpv가 만든 자식 창을 비활성화 → 클릭해도 키보드 포커스를 뺏지 않고 마우스는 부모(Qt)로 간다."""
    def cb(h, _):
        if user32.IsWindowEnabled(h):
            user32.EnableWindow(h, False)
        return True
    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)


def natural_key(path):
    # 전체 경로를 기준으로 자연 정렬 → 하위 폴더도 폴더별로 묶이고, 폴더 안에서는 파일명 순서대로 정렬됨
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path)]


def fmt_time(sec):
    if sec is None:
        return "--:--"
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def elide_middle(name, max_chars):
    """긴 파일명의 가운데를 ...으로 줄이되 확장자(뒷부분)는 남긴다."""
    if len(name) <= max_chars:
        return name
    keep = max_chars - 3
    tail = keep // 2
    return name[:keep - tail] + "..." + name[-tail:]


def fmt_size(num_bytes):
    if num_bytes is None:
        return "알 수 없음"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class MpvBridge(QObject):
    """mpv 이벤트 스레드 → Qt 메인 스레드 전달용"""
    eof = Signal()
    load_error = Signal()
    file_loaded = Signal()


class UpdateBridge(QObject):
    found = Signal(str)  # 새 버전 문자열 (백그라운드 확인 스레드 -> UI)


class VideoWidget(QWidget):
    doubleClicked = Signal()
    rightClicked = Signal()

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(0, 0, 0))
        self.setPalette(pal)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self._drag_start = None
        self._win_start = None
        self._dragged = False

    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            self.rightClicked.emit()
        elif ev.button() == Qt.LeftButton:
            self._drag_start = ev.globalPosition().toPoint()
            self._win_start = self.window().pos()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        # 화면을 클릭한 채 끌면 창 이동 (전체화면/최대화 상태에서는 이동 안 함)
        win = self.window()
        if (ev.buttons() & Qt.LeftButton) and self._drag_start is not None and not win.isFullScreen():
            cur = ev.globalPosition().toPoint()
            delta = cur - self._drag_start
            if delta.manhattanLength() > 4 or self._dragged:
                if win.isMaximized():
                    # 최대화 상태에서 끌면 원래 크기로 복원하고 커서 위치에 맞춰 창을 옮긴다
                    frac_x = (cur.x() - win.x()) / max(1, win.width())
                    win.showNormal()
                    self._win_start = cur - QPoint(int(win.width() * frac_x), 15)
                    self._drag_start = cur
                    delta = QPoint(0, 0)
                self._dragged = True
                win.move(self._win_start + delta)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_start = None
        self._dragged = False
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.doubleClicked.emit()


class PlaylistWidget(QListWidget):
    """오른쪽 재생목록 패널 - 여기로 끌어다 놓으면 (교체가 아니라) 추가된다."""
    filesDropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)  # 마우스 드래그/휠 스크롤이 부드럽게

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        urls = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.filesDropped.emit(urls)
        ev.acceptProposedAction()


class ClickSlider(QSlider):
    """클릭한 위치로 바로 이동하는 진행 바"""
    seekRequested = Signal(float, bool)  # 비율(0~1), 정확 탐색 여부

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setRange(0, 1000)
        self.setFocusPolicy(Qt.NoFocus)
        self.dragging = False

    def _frac(self, ev):
        return min(1.0, max(0.0, ev.position().x() / max(1, self.width())))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.dragging = True
            f = self._frac(ev)
            self.setValue(int(f * 1000))
            self.seekRequested.emit(f, False)

    def mouseMoveEvent(self, ev):
        if self.dragging:
            f = self._frac(ev)
            self.setValue(int(f * 1000))
            self.seekRequested.emit(f, False)

    def mouseReleaseEvent(self, ev):
        if self.dragging:
            self.dragging = False
            self.seekRequested.emit(self._frac(ev), True)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JYPlayer")
        self.resize(1280, 800)
        self.setAcceptDrops(True)

        self.settings = self._load_settings()
        self.rotations = self.settings.setdefault("rotation", {})
        self.random_mode = bool(self.settings.get("random", False))

        self.files = []   # 폴더 내(하위 폴더 포함) 동영상 (자연 정렬)
        self.order = []   # 재생 순서 (랜덤이면 섞인 순서)
        self.root_folder = ""  # 표시용 상대경로 계산 기준
        self.pos = -1
        self._now_playing_item = None  # 선택(selection)과 별개로 "현재 재생 중" 표시용
        self.pending_deletes = []  # [경로, 완전삭제 여부, 시도 횟수]
        self.consecutive_errors = 0
        self.hanja_held = False           # 한국어 키보드에서 오른쪽 Ctrl이 한자키로 잡히는 경우 대비
        self.hanja_release_seen = False
        self._msg_token = 0
        self._was_maximized = False
        self._last_cursor_pos = None
        self._cursor_idle_ticks = 0

        self._build_ui()

        self.bridge = MpvBridge()
        self.bridge.eof.connect(self._on_eof)
        self.bridge.load_error.connect(self._on_load_error)
        self.bridge.file_loaded.connect(self._on_file_loaded)
        self.update_bridge = UpdateBridge()
        self.update_bridge.found.connect(self._on_update_found)

        self.player = mpv.MPV(
            wid=str(int(self.video.winId())),
            vo="gpu",
            hwdec="auto-safe",
            keep_open="yes",
            idle="yes",
            input_default_bindings=False,
            input_vo_keyboard=False,
            input_cursor=False,
            window_dragging=False,
            cursor_autohide="no",
            ytdl=False,
            osd_font="Malgun Gothic",
            osd_font_size=OSD_FONT_SIZE,
            osd_duration=1200,
        )
        self.player.observe_property("eof-reached", self._mpv_eof_changed)
        self.player.register_event_callback(self._mpv_event)
        self.player.volume = self.settings.get("volume", 100)

        self.volume_save_timer = QTimer(self)
        self.volume_save_timer.setSingleShot(True)
        self.volume_save_timer.setInterval(500)
        self.volume_save_timer.timeout.connect(self._save_settings)

        self.del_timer = QTimer(self)
        self.del_timer.setInterval(100)
        self.del_timer.timeout.connect(self._process_deletes)

        self.tick = QTimer(self)
        self.tick.setInterval(200)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start()

        geo = self.settings.get("geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        self._update_mode_label()
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(4000, self._start_update_check)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.video = VideoWidget()
        self.video.doubleClicked.connect(self.toggle_fullscreen)
        self.video.rightClicked.connect(self._show_file_info)

        self.btn_play = QToolButton()
        self.btn_play.setText("▶")
        self.btn_play.setFocusPolicy(Qt.NoFocus)
        self.btn_play.setToolTip("재생/일시정지")
        self.btn_play.clicked.connect(self.toggle_pause)
        self.btn_stop = QToolButton()
        self.btn_stop.setText("■")
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.setToolTip("정지")
        self.btn_stop.clicked.connect(self.stop_playback)
        self.slider = ClickSlider()
        self.slider.seekRequested.connect(self._seek_fraction)
        self.lbl_time = QLabel("--:-- / --:--")

        self.lbl_name = QLabel("폴더를 열거나 이 창에 끌어다 놓으세요")
        self.lbl_name.setMinimumWidth(50)
        self.lbl_mode = QLabel("")
        self.lbl_mode.setObjectName("mode")
        self.lbl_msg = QLabel("")
        self.lbl_msg.setObjectName("msg")
        self.lbl_index = QLabel("0 / 0")
        self.chk_random = QCheckBox("랜덤")
        self.chk_random.setFocusPolicy(Qt.NoFocus)
        self.chk_random.toggled.connect(self._on_random_checkbox)
        self.btn_open = QToolButton()
        self.btn_open.setText("폴더 열기")
        self.btn_open.setFocusPolicy(Qt.NoFocus)
        self.btn_open.clicked.connect(self.choose_folder)
        self.btn_update = QToolButton()
        self.btn_update.setText("업데이트 확인")
        self.btn_update.setFocusPolicy(Qt.NoFocus)
        self.btn_update.clicked.connect(self.on_update_clicked)

        row1 = QHBoxLayout()
        row1.addWidget(self.btn_play)
        row1.addWidget(self.btn_stop)
        row1.addWidget(self.slider, 1)
        row1.addWidget(self.lbl_time)
        row2 = QHBoxLayout()
        row2.addWidget(self.lbl_name, 1)
        row2.addWidget(self.lbl_msg)
        row2.addWidget(self.lbl_mode)
        row2.addWidget(self.lbl_index)
        row2.addWidget(self.chk_random)
        row2.addWidget(self.btn_open)
        row2.addWidget(self.btn_update)

        self.bar = QWidget()
        self.bar.setObjectName("bar")
        bar_layout = QVBoxLayout(self.bar)
        bar_layout.setContentsMargins(10, 6, 10, 6)
        bar_layout.setSpacing(4)
        bar_layout.addLayout(row1)
        bar_layout.addLayout(row2)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(self.video, 1)
        left_layout.addWidget(self.bar)

        self.listw = PlaylistWidget()
        self.listw.setFocusPolicy(Qt.NoFocus)
        self.listw.itemDoubleClicked.connect(self._on_list_double_click)
        self.listw.filesDropped.connect(self.add_to_playlist)

        def mkbtn(text, slot):
            b = QToolButton()
            b.setText(text)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(slot)
            return b

        self.btn_add = mkbtn("추가", self._pick_files_to_add)
        self.btn_remove = mkbtn("제거", self.remove_selected_from_playlist)
        self.btn_save_list = mkbtn("목록 저장", self.save_playlist)
        self.btn_load_list = mkbtn("목록 열기", self.load_playlist)
        list_tools = QHBoxLayout()
        list_tools.addWidget(self.btn_add)
        list_tools.addWidget(self.btn_remove)
        list_tools.addWidget(self.btn_save_list)
        list_tools.addWidget(self.btn_load_list)

        self.right_panel = QWidget()
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.addLayout(list_tools)
        right_layout.addWidget(self.listw, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1000, 260])
        self.setCentralWidget(self.splitter)

        self.setStyleSheet("""
            QMainWindow, QSplitter, QWidget#bar { background: #1b1b1b; }
            QLabel, QCheckBox { color: #dddddd; }
            QLabel#mode { color: #ffc861; padding: 0 8px; }
            QLabel#msg { color: #8fd0ff; padding: 0 8px; }
            QListWidget { background: #141414; color: #bbbbbb; border: none; }
            QListWidget::item:selected { background: #2f4f6f; color: white; }
            QListWidget::item { padding: 2px 0; }
            QScrollBar:vertical { background: #1b1b1b; width: 14px; margin: 0; }
            QScrollBar::handle:vertical { background: #555555; min-height: 24px; border-radius: 6px; }
            QScrollBar::handle:vertical:hover { background: #6f6f6f; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QToolButton { color: #dddddd; background: #2a2a2a; border: 1px solid #3a3a3a; padding: 2px 8px; }
            QSlider::groove:horizontal { height: 6px; background: #3a3a3a; }
            QSlider::sub-page:horizontal { background: #4a90d9; }
            QSlider::handle:horizontal { width: 12px; margin: -4px 0; background: #dddddd; border-radius: 6px; }
        """)

    # ---------------------------------------------------------------- 설정
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        self.settings["random"] = self.random_mode
        tmp = SETTINGS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=1)
            os.replace(tmp, SETTINGS_PATH)
        except OSError:
            pass

    # ---------------------------------------------------------------- 재생목록
    @property
    def current(self):
        return self.order[self.pos] if 0 <= self.pos < len(self.order) else None

    def choose_folder(self):
        start = self.settings.get("last_folder", "")
        folder = QFileDialog.getExistingDirectory(self, "동영상 폴더 열기", start)
        if folder:
            self.open_path(folder)

    def restore_last_session(self):
        """더블클릭으로 그냥 실행했을 때 - 폴더 선택 창 대신 지난번 재생목록을 그대로 불러온다."""
        saved = self.settings.get("last_playlist") or []
        files = [p for p in saved if os.path.isfile(p)]
        if not files:
            self.flash("폴더를 열거나 이 창에 끌어다 놓으세요")
            return
        self.root_folder = self.settings.get("last_root_folder", "")
        start = self.settings.get("last_current")
        if start not in files:
            start = None
        self._set_playlist(files, start=start, autoplay=False)

    @staticmethod
    def _scan_videos(path):
        """폴더면 하위 폴더까지 재귀적으로, 파일이면 그 파일 하나를 동영상 목록으로 반환."""
        if os.path.isdir(path):
            found = []
            for root, _dirs, names in os.walk(path):
                for n in names:
                    if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                        found.append(os.path.join(root, n))
            return found
        if os.path.isfile(path) and os.path.splitext(path)[1].lower() in VIDEO_EXTS:
            return [path]
        return []

    def open_path(self, path):
        path = os.path.abspath(path)
        if os.path.isdir(path):
            folder, start = path, None
        elif os.path.isfile(path):
            folder, start = os.path.dirname(path), path
        else:
            return
        if not os.path.isdir(folder):
            self.flash("폴더를 열 수 없음")
            return
        files = self._scan_videos(folder)
        if start and start not in files:
            files.append(start)
        files.sort(key=natural_key)
        self.root_folder = folder
        self.settings["last_folder"] = folder
        self._save_settings()
        self._set_playlist(files, start=start)

    def _set_playlist(self, files, start=None, autoplay=True):
        """전달된 목록으로 재생목록을 통째로 교체한다 (폴더 열기 / 재생목록 불러오기 공용)."""
        self.files = files
        self._rebuild_list()
        if not files:
            self.order, self.pos = [], -1
            self.player.command("stop")
            self._update_labels()
            self.flash("동영상 파일이 없습니다")
            return
        if self.random_mode:
            first = start or random.choice(files)
            rest = [p for p in files if p != first]
            random.shuffle(rest)
            self.order, self.pos = [first] + rest, 0
        else:
            self.order = files[:]
            self.pos = files.index(start) if start else 0
        if autoplay:
            self.play_current()
        else:
            self._update_labels()  # 목록과 현재 위치만 복원하고 재생은 사용자가 시작할 때까지 대기

    def _display_name(self, path):
        """하위 폴더 파일은 상대경로로 표시해 같은 이름의 파일을 구분한다."""
        try:
            rel = os.path.relpath(path, self.root_folder)
        except ValueError:
            rel = os.path.basename(path)
        return rel

    def _rebuild_list(self):
        self.listw.clear()
        self._now_playing_item = None  # clear()가 기존 아이템을 C++ 쪽에서 파괴하므로 참조도 함께 정리
        for p in self.files:
            item = QListWidgetItem(self._display_name(p))
            item.setData(Qt.UserRole, p)
            item.setToolTip(p)
            self.listw.addItem(item)

    def _mark_now_playing(self, item):
        """재생 중 표시는 글자 굵기/색으로 - Qt의 selection과 분리해서, 재생목록 항목을 선택해 지울 때
        재생 중인 항목까지 같이 선택된 것으로 오인해 지워지는 일이 없도록 한다."""
        if self._now_playing_item is not None:
            font = self._now_playing_item.font()
            font.setBold(False)
            self._now_playing_item.setFont(font)
            self._now_playing_item.setForeground(self.listw.palette().text())
        self._now_playing_item = item
        if item is not None:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setForeground(QColor("#8fd0ff"))

    def _on_list_double_click(self, item):
        path = item.data(Qt.UserRole)
        if path in self.order:
            self.pos = self.order.index(path)
            self.play_current()

    def play_current(self):
        path = self.current
        if not path:
            return
        self.player["video-rotate"] = self.rotations.get(path, 0)
        self.player.loadfile(path)
        self.player.pause = False
        self._update_labels()

    def next_video(self, auto=False):
        if not self.order:
            return
        if self.pos + 1 < len(self.order):
            self.pos += 1
        elif self.random_mode and len(self.order) > 1:
            cur = self.current
            rest = [p for p in self.files if p != cur]
            random.shuffle(rest)
            self.order, self.pos = [cur] + rest, 1
        else:
            self.flash("마지막 영상입니다")
            return
        self.play_current()

    def prev_video(self):
        if self.pos > 0:
            self.pos -= 1
            self.play_current()
        elif self.order:
            self.flash("첫 영상입니다")

    def toggle_random(self):
        self.random_mode = not self.random_mode
        cur = self.current
        if self.random_mode:
            rest = [p for p in self.files if p != cur]
            random.shuffle(rest)
            self.order = ([cur] if cur else []) + rest
            self.pos = 0 if self.order else -1
        else:
            self.order = self.files[:]
            self.pos = self.files.index(cur) if cur in self.files else (0 if self.files else -1)
        self._save_settings()
        self._update_mode_label()
        self.flash("랜덤 재생 ON" if self.random_mode else "랜덤 재생 OFF")

    def _on_random_checkbox(self, _checked):
        self.toggle_random()

    # ---------------------------------------------------------------- 재생목록 편집
    def _pick_files_to_add(self):
        filt = "동영상 파일 (" + " ".join("*" + e for e in sorted(VIDEO_EXTS)) + ")"
        start_dir = self.root_folder or self.settings.get("last_folder", "")
        paths, _ = QFileDialog.getOpenFileNames(self, "재생목록에 추가", start_dir, filt)
        if paths:
            self.add_to_playlist(paths)

    def add_to_playlist(self, paths):
        """드래그로 끌어온 파일/폴더 또는 '추가' 버튼으로 고른 파일들을 재생목록 끝에 붙인다."""
        new_paths = []
        for p in paths:
            for v in self._scan_videos(os.path.abspath(p)):
                if v not in self.files and v not in new_paths:
                    new_paths.append(v)
        if not new_paths:
            self.flash("추가할 새 동영상이 없습니다")
            return
        new_paths.sort(key=natural_key)
        self.files.extend(new_paths)
        for p in new_paths:
            item = QListWidgetItem(self._display_name(p))
            item.setData(Qt.UserRole, p)
            item.setToolTip(p)
            self.listw.addItem(item)
        if self.random_mode:
            insert_at = self.pos + 1 if self.pos >= 0 else len(self.order)
            shuffled = new_paths[:]
            random.shuffle(shuffled)
            self.order[insert_at:insert_at] = shuffled
        else:
            self.order.extend(new_paths)
        if self.pos < 0 and self.order:
            self.pos = 0
            self.play_current()
        self._update_labels()
        self.flash(f"{len(new_paths)}개 재생목록에 추가")

    def _selected_paths(self):
        rows = sorted({idx.row() for idx in self.listw.selectedIndexes()})
        return [self.files[r] for r in rows]

    def copy_selected_to_clipboard(self):
        """Ctrl+C - 선택한 항목의 파일 경로를 클립보드에 복사."""
        paths = self._selected_paths()
        if not paths:
            self.flash("선택된 항목이 없습니다")
            return
        QApplication.clipboard().setText("\n".join(paths))
        self.flash(f"{len(paths)}개 경로 복사됨")

    def cut_selected_from_playlist(self):
        """Ctrl+X - 경로를 클립보드에 복사하고 재생목록에서 뺌 (디스크 파일은 안 지움)."""
        paths = self._selected_paths()
        if not paths:
            self.flash("선택된 항목이 없습니다")
            return
        QApplication.clipboard().setText("\n".join(paths))
        self.remove_selected_from_playlist()
        self.flash(f"{len(paths)}개 잘라냄")

    def remove_selected_from_playlist(self):
        """선택한 항목을 재생목록에서만 뺀다 (디스크 파일은 그대로 둠)."""
        rows = sorted({idx.row() for idx in self.listw.selectedIndexes()}, reverse=True)
        if not rows:
            self.flash("제거할 항목을 먼저 선택하세요")
            return
        removed_current = False
        for row in rows:
            path = self.files.pop(row)
            self.listw.takeItem(row)
            self.rotations.pop(path, None)
            if path in self.order:
                idx = self.order.index(path)
                self.order.pop(idx)
                if idx < self.pos:
                    self.pos -= 1
                elif idx == self.pos:
                    removed_current = True
        if not self.order:
            self.pos = -1
            self.player.command("stop")
        elif removed_current:
            self.pos = min(self.pos, len(self.order) - 1)
            self.play_current()
        self._update_labels()
        self.flash(f"{len(rows)}개 재생목록에서 제거")

    def save_playlist(self):
        if not self.files:
            self.flash("저장할 재생목록이 없습니다")
            return
        start_dir = self.settings.get("last_playlist_dir", self.root_folder)
        path, _ = QFileDialog.getSaveFileName(self, "재생목록 저장", start_dir, "M3U8 재생목록 (*.m3u8)")
        if not path:
            return
        if not path.lower().endswith((".m3u8", ".m3u")):
            path += ".m3u8"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for p in self.files:
                    f.write(p + "\n")
        except OSError as e:
            self.flash(f"저장 실패: {e}", error=True)
            return
        self.settings["last_playlist_dir"] = os.path.dirname(path)
        self._save_settings()
        self.flash("재생목록 저장됨: " + os.path.basename(path))

    def load_playlist(self):
        start_dir = self.settings.get("last_playlist_dir", self.root_folder)
        path, _ = QFileDialog.getOpenFileName(self, "재생목록 열기", start_dir, "재생목록 (*.m3u *.m3u8 *.txt)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            self.flash(f"열기 실패: {e}", error=True)
            return
        files = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if os.path.isfile(line):
                files.append(os.path.abspath(line))
        if not files:
            self.flash("재생목록에 유효한 파일이 없습니다", error=True)
            return
        self.root_folder = os.path.dirname(path)
        self.settings["last_playlist_dir"] = self.root_folder
        self._save_settings()
        self._set_playlist(files)
        self.flash(f"재생목록 열림: {os.path.basename(path)} ({len(files)}개)")

    # ---------------------------------------------------------------- 삭제
    def delete_current(self, permanent):
        path = self.current
        if not path:
            return
        # 1) 목록에서 빼고 다음 영상으로 먼저 전환 (재생 중 파일 잠금 회피)
        row = self.files.index(path) if path in self.files else -1
        if row >= 0:
            self.files.pop(row)
            self.listw.takeItem(row)
        self.order.pop(self.pos)
        if self.rotations.pop(path, None) is not None:
            self._save_settings()

        if not self.order:
            self.pos = -1
            self.player.command("stop")
        else:
            if self.pos >= len(self.order):
                if self.random_mode:
                    self.order = self.files[:]
                    random.shuffle(self.order)
                    self.pos = 0
                else:
                    self.pos = len(self.order) - 1
            self.play_current()
        self._update_labels()

        # 2) 잠금이 풀릴 때까지 재시도하며 삭제
        self.pending_deletes.append([path, permanent, 0])
        self._process_deletes()
        if self.pending_deletes:
            self.del_timer.start()

    def _process_deletes(self):
        remaining = []
        for item in self.pending_deletes:
            path, permanent, tries = item
            name = os.path.basename(path)
            try:
                if permanent:
                    os.remove(path)
                else:
                    send2trash(path)
                self.flash(("완전 삭제: " if permanent else "휴지통으로: ") + name)
            except FileNotFoundError:
                pass
            except OSError as e:
                item[2] = tries + 1
                if item[2] < 50:  # 약 5초
                    remaining.append(item)
                else:
                    self.flash(f"삭제 실패: {name} ({e})", error=True)
        self.pending_deletes = remaining
        if not remaining:
            self.del_timer.stop()

    # ---------------------------------------------------------------- 재생 조작
    def seek_relative(self, seconds):
        if not self.current:
            return
        t = self.player.time_pos
        d = self.player.duration
        self.player.seek(seconds, "relative", "exact")
        if t is not None:
            target = t + seconds
            if d:
                target = min(target, d)
            sign = "+" if seconds > 0 else "-"
            amount = f"{abs(seconds) // 60}분" if abs(seconds) >= 60 else f"{abs(seconds)}초"
            pct = f"  {max(0, target) / d * 100:.0f}%" if d else ""
            # 파일명은 % 옆에 한 줄로 붙이고, 화면 폭에 안 들어가면 가운데를 ...으로 줄인다
            # (OSD 글자 크기는 화면 높이의 OSD_FONT_SIZE/720)
            head = f"{sign}{amount}  {fmt_time(target)} / {fmt_time(d)}{pct}  "
            font = QFont("Malgun Gothic")
            font.setPixelSize(max(1, int(self.video.height() * OSD_FONT_SIZE / 720)))
            fm = QFontMetrics(font)
            room = int(self.video.width() * 0.92) - fm.horizontalAdvance(head)
            name = os.path.basename(self.current)
            # 실제 글자 폭을 재서, 화면 폭에 들어올 때까지만 가운데를 줄인다
            while len(name) > 10 and fm.horizontalAdvance(name) > room:
                name = elide_middle(name, len(name) - 1)
            self.player.show_text(head + name, "1000")

    def _seek_fraction(self, frac, exact):
        if self.current:
            self.player.seek(frac * 100, "absolute-percent", "exact" if exact else "keyframes")

    def change_volume(self, delta):
        vol = max(0, min(VOLUME_MAX, round(self.player.volume) + delta))
        self.player.volume = vol
        self.settings["volume"] = vol
        self.volume_save_timer.start()  # 연타/길게 누름 중 매번 디스크에 쓰지 않도록 묶어서 저장
        self.player.show_text(f"볼륨 {vol}%", "800")
        self._update_mode_label()

    def change_zoom(self, delta):
        """delta=None 이면 원래 크기로."""
        if not self.current:
            return
        zoom = 0.0 if delta is None else max(-2.0, min(3.0, (self._mpv_get("video_zoom") or 0.0) + delta))
        self.player["video-zoom"] = zoom
        if abs(zoom) < 0.005:
            self.player["video-pan-x"] = 0
            self.player["video-pan-y"] = 0
        self.player.show_text(f"확대 {2 ** zoom * 100:.0f}%", "800")

    def toggle_pause(self):
        if not self.current:
            return
        if not self._mpv_get("path"):
            # 정지(stop) 상태라 아무것도 로드돼 있지 않음 -> 같은 파일을 다시 재생
            self.play_current()
            return
        self.player.pause = not self.player.pause
        self.player.show_text("일시정지" if self.player.pause else "재생", "800")

    def stop_playback(self):
        """일시정지와 달리 재생을 완전히 멈춘다 (다시 재생하면 처음부터)."""
        if not self.current:
            return
        self.player.command("stop")
        self.flash("정지")

    def rotate(self, delta):
        path = self.current
        if not path:
            return
        deg = (self.rotations.get(path, 0) + delta) % 360
        if deg:
            self.rotations[path] = deg
        else:
            self.rotations.pop(path, None)
        self.player["video-rotate"] = deg
        self._save_settings()
        self._update_mode_label()
        self.player.show_text(f"회전 {deg}°", "1000")

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._was_maximized = self.isMaximized()
            self.bar.hide()
            self.right_panel.hide()
            self.showFullScreen()

    def exit_fullscreen(self):
        if not self.isFullScreen():
            return
        self.bar.show()
        self.right_panel.show()
        self.video.unsetCursor()
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    # ---------------------------------------------------------------- 키 입력
    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QEvent.KeyRelease:
            if ev.nativeVirtualKey() == VK_HANJA and not ev.isAutoRepeat():
                self.hanja_held = False
                self.hanja_release_seen = True
            return False
        if et == QEvent.WindowDeactivate and obj is self:
            self.hanja_held = False
            return False
        if et != QEvent.KeyPress or QApplication.activeModalWidget() is not None:
            return False
        return self.handle_key(ev)

    def handle_key(self, ev):
        vk = ev.nativeVirtualKey()
        if vk == VK_HANJA:
            self.hanja_held = True
            return True
        qmods = ev.modifiers()
        shift = bool(qmods & Qt.ShiftModifier) or key_down(VK_SHIFT)
        # 한자키(=한국어 키보드의 오른쪽 Ctrl)는 눌림 이벤트만 믿으면 안 떼졌을 때 계속 눌린 걸로 남을 수 있어서,
        # 실제 키 상태(GetAsyncKeyState)가 지금 눌려 있을 때만 Ctrl로 취급한다.
        ctrl = (bool(qmods & Qt.ControlModifier) or key_down(VK_CONTROL)
                or (self.hanja_held and async_key_down(VK_HANJA)))

        if vk in (VK_LEFT, VK_RIGHT):
            step = SEEK_STEPS[(ctrl, shift)]
            self.seek_relative(step if vk == VK_RIGHT else -step)
        elif vk in (VK_UP, VK_DOWN):
            self.change_volume(VOLUME_STEP if vk == VK_UP else -VOLUME_STEP)
        elif vk == VK_ADD:
            self.change_zoom(ZOOM_STEP)
        elif vk == VK_SUBTRACT:
            self.change_zoom(-ZOOM_STEP)
        elif vk == VK_MULTIPLY:
            self.change_zoom(None)
        elif vk == VK_PRIOR:
            self.prev_video()
        elif vk == VK_NEXT:
            self.next_video()
        elif vk == VK_DELETE:
            if not ev.isAutoRepeat():
                self.delete_current(permanent=shift)
        elif vk == VK_SPACE:
            if not ev.isAutoRepeat():
                self.toggle_pause()
        elif vk == VK_R:
            if not ev.isAutoRepeat():
                self.toggle_random()
        elif vk == VK_C and ctrl:
            if not ev.isAutoRepeat():
                self.copy_selected_to_clipboard()
        elif vk == VK_X and ctrl:
            if not ev.isAutoRepeat():
                self.cut_selected_from_playlist()
        elif vk == VK_P:
            if not ev.isAutoRepeat():
                self.rotate(-90 if shift else 90)
        elif vk in (VK_F, VK_RETURN):
            if not ev.isAutoRepeat():
                self.toggle_fullscreen()
        elif vk == VK_ESCAPE:
            self.exit_fullscreen()
        else:
            return False
        return True

    # ---------------------------------------------------------------- mpv 이벤트
    def _mpv_eof_changed(self, _name, value):
        if value:
            self.bridge.eof.emit()

    def _mpv_event(self, event):
        eid = event.event_id.value
        if eid == mpv.MpvEventID.END_FILE:
            if event.data.reason == mpv.MpvEventEndFile.ERROR:
                self.bridge.load_error.emit()
        elif eid == mpv.MpvEventID.FILE_LOADED:
            self.bridge.file_loaded.emit()

    def _on_eof(self):
        if self.current and self.player.eof_reached:
            self.next_video(auto=True)

    def _on_load_error(self):
        name = os.path.basename(self.current or "")
        self.consecutive_errors += 1
        if self.consecutive_errors > len(self.order):
            self.flash("재생할 수 있는 영상이 없습니다", error=True)
            return
        self.flash(f"재생 실패: {name}", error=True)
        self.next_video(auto=True)

    def _on_file_loaded(self):
        self.consecutive_errors = 0
        disable_child_windows(int(self.video.winId()))

    def _mpv_get(self, name, default=None):
        try:
            return getattr(self.player, name)
        except Exception:
            return default

    def _show_file_info(self):
        path = self.current
        if not path:
            self.flash("재생 중인 영상이 없습니다")
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        w, h = self._mpv_get("width"), self._mpv_get("height")
        dur = self._mpv_get("duration")
        vcodec = self._mpv_get("video_codec") or "알 수 없음"
        acodec = self._mpv_get("audio_codec") or "없음"
        fps = self._mpv_get("container_fps") or self._mpv_get("estimated_vf_fps")
        vbr = abr = None
        for tr in self._mpv_get("track_list") or []:
            br = tr.get("demux-bitrate")
            if tr.get("type") == "video" and vbr is None:
                vbr = br
            elif tr.get("type") == "audio" and abr is None:
                abr = br
        total_br = size * 8 / dur if size and dur else None

        def fmt_br(bps):
            if not bps:
                return "알 수 없음"
            return f"{bps / 1000:,.0f} kbps"

        lines = [
            f"파일: {os.path.basename(path)}",
            f"경로: {path}",
            f"크기: {fmt_size(size)}",
            f"재생시간: {fmt_time(dur)}",
            f"해상도: {w}x{h}" if w and h else "해상도: 알 수 없음",
            f"영상 코덱: {vcodec}",
            f"음성 코덱: {acodec}",
            f"FPS: {fps:.2f}" if fps else "FPS: 알 수 없음",
            f"비트레이트: {fmt_br(total_br)} (전체 평균)",
            f"  영상: {fmt_br(vbr)}   음성: {fmt_br(abr)}",
        ]
        box = QMessageBox(self)
        box.setWindowTitle("파일 정보")
        box.setIcon(QMessageBox.NoIcon)
        box.setText("\n".join(lines))
        # Windows 다크 모드에서 기본 QMessageBox 글자가 흐릿하게(밝은 회색) 나오는 문제 방지 - 명시적으로 검정 글씨 강제
        box.setStyleSheet("QMessageBox { background: #f2f2f2; } QLabel { color: #000000; }")
        box.exec()

    # ---------------------------------------------------------------- 업데이트
    def _start_update_check(self):
        """켤 때 한 번, 조용히 확인. gh 가 없거나 로그인이 안 돼 있으면 아무것도 하지 않는다."""
        cur = jyupdate.installed_version()
        if not cur:
            return

        def work():
            gh = jyupdate.find_gh()
            if gh and jyupdate.logged_in(gh):
                latest = jyupdate.latest_version(gh)
                if latest and jyupdate.is_newer(latest, cur):
                    self.update_bridge.found.emit(latest)

        threading.Thread(target=work, daemon=True).start()

    def _on_update_found(self, latest):
        self.btn_update.setText(f"새 버전 v{latest} 업데이트")
        self.btn_update.setStyleSheet("QToolButton { color: #1b1b1b; background: #ffc861; font-weight: bold; }")
        self.flash(f"새 버전 v{latest} 이 있습니다 (오른쪽 아래 업데이트 버튼)")

    def _busy(self, fn):
        """fn 을 스레드에서 실행하는 동안 화면이 멈추지 않게 이벤트를 처리하고 결과를 돌려준다."""
        box = {}

        def work():
            try:
                box["r"] = fn()
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            while t.is_alive():
                QApplication.processEvents()
                t.join(0.03)
        finally:
            QApplication.restoreOverrideCursor()
        return box.get("r")

    def _ask(self, text):
        return QMessageBox.question(self, "JYPlayer 업데이트", text) == QMessageBox.Yes

    def _warn(self, text):
        QMessageBox.warning(self, "JYPlayer 업데이트", text)

    def on_update_clicked(self):
        self.btn_update.setEnabled(False)
        try:
            self._do_update()
        finally:
            self.btn_update.setEnabled(True)

    def _do_update(self):
        cur = jyupdate.installed_version()
        if not cur:
            QMessageBox.information(self, "JYPlayer 업데이트", "설치된 JYsubTools 에서만 업데이트할 수 있습니다.")
            return
        gh = jyupdate.find_gh()
        if not gh:
            if not self._ask("업데이트에는 GitHub CLI(gh)가 필요합니다.\n지금 설치할까요? (콘솔 창이 열립니다)"):
                return
            self._busy(jyupdate.install_gh_in_console)
            gh = jyupdate.find_gh()
            if not gh:
                self._warn("gh 를 찾지 못했습니다. 설치가 끝났다면 JYPlayer 를 다시 켠 뒤 시도하세요.")
                return
        if not self._busy(lambda: jyupdate.logged_in(gh)):
            if not self._ask("GitHub 로그인이 필요합니다.\n콘솔 창이 열리면 안내에 따라 로그인하세요. 열까요?"):
                return
            self._busy(lambda: jyupdate.login_in_console(gh))
            if not self._busy(lambda: jyupdate.logged_in(gh)):
                self._warn("로그인이 완료되지 않았습니다.")
                return
        latest = self._busy(lambda: jyupdate.latest_version(gh))
        if not latest:
            self._warn("최신 버전을 확인하지 못했습니다. (인터넷 연결이나 저장소 접근 권한을 확인하세요)")
            return
        if not jyupdate.is_newer(latest, cur):
            QMessageBox.information(self, "JYPlayer 업데이트", f"이미 최신 버전입니다. (v{cur})")
            return
        if not self._ask(f"v{cur} → v{latest} 로 업데이트할까요?\n받은 뒤 JYPlayer 가 종료되고 다시 시작됩니다."):
            return
        self.flash("업데이트 파일을 받는 중...")
        setup = self._busy(lambda: jyupdate.download_setup(gh, latest))
        if not setup:
            self._warn("업데이트 파일을 받지 못했습니다.")
            return
        jyupdate.launch_setup(setup, "jyplayer")
        self.close()
    # ---------------------------------------------------------------- 표시
    def flash(self, msg, error=False):
        self._msg_token += 1
        token = self._msg_token
        self.lbl_msg.setStyleSheet("color: #ff8080;" if error else "")
        self.lbl_msg.setText(msg)
        try:
            self.player.show_text(msg, "1500")
        except Exception:
            pass
        QTimer.singleShot(3000, lambda: token == self._msg_token and self.lbl_msg.setText(""))

    def _update_labels(self):
        path = self.current
        if path:
            name = self._display_name(path)
            self.lbl_name.setText(name)
            self.setWindowTitle(f"{os.path.basename(path)} - JYPlayer")
            idx = self.files.index(path) + 1 if path in self.files else 0
            self.lbl_index.setText(f"{idx} / {len(self.files)}")
            self._mark_now_playing(self.listw.item(idx - 1) if idx else None)
            if idx:
                self.listw.scrollToItem(self.listw.item(idx - 1))
        else:
            self.lbl_name.setText("재생 중인 영상 없음")
            self.setWindowTitle("JYPlayer")
            self.lbl_index.setText(f"0 / {len(self.files)}")
            self.lbl_time.setText("--:-- / --:--")
            self.slider.setValue(0)
        self._update_mode_label()

    def _update_mode_label(self):
        parts = []
        deg = self.rotations.get(self.current, 0) if self.current else 0
        if deg:
            parts.append(f"회전 {deg}°")
        vol = round(self.player.volume) if hasattr(self, "player") else 100
        if vol != 100:
            parts.append(f"볼륨 {vol}%")
        self.lbl_mode.setText("  ".join(parts))
        self.chk_random.blockSignals(True)
        self.chk_random.setChecked(self.random_mode)
        self.chk_random.blockSignals(False)

    def _on_tick(self):
        disable_child_windows(int(self.video.winId()))
        if self.current:
            try:
                t, d, paused = self.player.time_pos, self.player.duration, self.player.pause
            except Exception:
                t = d = paused = None
            pct = f"  ({t / d * 100:.0f}%)" if d and t is not None else ""
            self.lbl_time.setText(f"{fmt_time(t)} / {fmt_time(d)}{pct}")
            # 버튼은 "누르면 일어날 동작"을 보여줌: 재생 중엔 일시정지 아이콘, 멈춰 있으면 재생 아이콘
            self.btn_play.setText("▶" if (t is None or paused) else "❚❚")
            if d and t is not None and not self.slider.dragging:
                self.slider.setValue(int(t / d * 1000))
        else:
            self.btn_play.setText("▶")

        # 전체화면에서 마우스가 2초간 멈추면 커서 숨김
        if self.isFullScreen():
            pos = QCursor.pos()
            if pos != self._last_cursor_pos:
                self._last_cursor_pos = pos
                self._cursor_idle_ticks = 0
                self.video.unsetCursor()
            else:
                self._cursor_idle_ticks += 1
                if self._cursor_idle_ticks == 10:
                    self.video.setCursor(Qt.BlankCursor)

    # ---------------------------------------------------------------- 드래그 앤 드롭 / 종료
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        urls = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.open_path(urls[0])

    def closeEvent(self, ev):
        self.exit_fullscreen()
        self._process_deletes()
        self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.settings["last_playlist"] = self.files
        self.settings["last_current"] = self.current
        self.settings["last_root_folder"] = self.root_folder
        self._save_settings()
        try:
            self.player.terminate()
        except Exception:
            pass
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Malgun Gothic", 10))
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.open_path(sys.argv[1])
    else:
        QTimer.singleShot(0, win.restore_last_session)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

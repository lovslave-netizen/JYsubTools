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
for _d in (resource_dir(), os.path.normpath(os.path.join(app_dir(), "..", "bin")),
           os.path.join(os.environ.get("LOCALAPPDATA", ""), "JY", "bin")):     # 공용 bin (JY_Encoder 와 같이 씀)
    if os.path.isdir(_d):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_d)

_COMMON = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "common")
if os.path.isdir(_COMMON):  # 개발 중(스크립트 실행)에는 repo 의 common 폴더를 사용
    sys.path.insert(0, _COMMON)
import tempfile  # noqa: E402
import jyupdate  # noqa: E402
import mpv  # noqa: E402
from send2trash import send2trash  # noqa: E402
from PySide6.QtCore import QByteArray, QDir, QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFileSystemModel, QHBoxLayout, QLabel, QListView,
    QListWidget, QPushButton, QVBoxLayout,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QSizePolicy, QSlider, QSplitter, QStyledItemDelegate, QToolButton,
    QVBoxLayout, QWidget,
)

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".rm", ".rmvb", ".asf", ".divx",
}
SETTINGS_PATH = os.path.join(app_dir(), "JYPlayer_settings.json")

# Windows 가상키 코드 - 키보드 배열/한글 입력 상태와 무관하게 동작시키기 위해 사용
VK_SHIFT, VK_CONTROL, VK_HANJA = 0x10, 0x11, 0x19
VK_HANGUL = 0x15  # 한국 키보드의 오른쪽 Alt (한/영 키)
VK_ESCAPE, VK_RETURN, VK_SPACE, VK_PRIOR, VK_NEXT = 0x1B, 0x0D, 0x20, 0x21, 0x22
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_DELETE = 0x25, 0x26, 0x27, 0x28, 0x2E
VK_F, VK_P, VK_R = 0x46, 0x50, 0x52
VK_OEM_4 = 0xDB  # [ 키
VK_J = 0x4A
VK_OEM_COMMA, VK_OEM_PERIOD = 0xBC, 0xBE  # , . 키
VK_C, VK_X = 0x43, 0x58
VK_MULTIPLY, VK_ADD, VK_SUBTRACT = 0x6A, 0x6B, 0x6D  # 숫자패드 * + -
VK_NUMPAD2, VK_NUMPAD4, VK_NUMPAD6, VK_NUMPAD8 = 0x62, 0x64, 0x66, 0x68  # 숫자패드 2 4 6 8 (NumLock 켜진 상태)
ZOOM_STEP = 0.1  # mpv video-zoom 은 로그(2^x) 단위
PAN_STEP = 0.05  # 화면 크기 대비 비율 (확대했을 때만 움직임)
OSD_FONT_SIZE = 22  # 화면에 뜨는 안내 글자 크기 (720p 기준)

# (Ctrl, Shift) -> 이동 초
SEEK_STEPS = {(False, False): 5, (False, True): 10, (True, False): 30, (True, True): 60}
VOLUME_STEP = 5
VOLUME_MAX = 130
AUDIO_DELAY_STEP = 0.1  # 소리 싱크 조절 단위(초). . 은 소리를 늦춤, , 는 앞당김
# 하드웨어 디코딩 선택지 (표시 이름, mpv hwdec 값). 자동-안전은 GPU 가 되는 경우만 쓰고 안 되면 CPU
HWDEC_CHOICES = [("자동 (권장)", "auto-safe"), ("자동 (모든 방식)", "auto"), ("끔 (CPU 디코딩)", "no"),
                 ("Direct3D 11", "d3d11va"), ("DXVA2", "dxva2")]
# 미리 읽기(디스크에서 미리 가져다 두는 양, MB). 느린 디스크(내장 HDD 등)에서 재생/구간 이동을 부드럽게
READAHEAD_CHOICES = [("2GB (기본, 최대)", 2048), ("1GB", 1024), ("500MB", 500), ("150MB", 150)]

user32 = ctypes.windll.user32
user32.GetKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.restype = ctypes.c_short
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def key_down(vk):
    return bool(user32.GetKeyState(vk) & 0x8000)


def async_key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020


def disable_child_windows(hwnd):
    """mpv가 만든 자식 창 처리.
    - 클릭 통과(WS_EX_TRANSPARENT): 자식 창이 클릭을 가로채지 않아서 마우스가 Qt(영상 영역)로 온다 → 화면 드래그/더블클릭/우클릭 동작
    - 비활성화(EnableWindow False): 클릭해도 키보드 포커스를 뺏지 않는다"""
    def cb(h, _):
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        if not ex & WS_EX_TRANSPARENT:
            user32.SetWindowLongW(h, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT)
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
    pressed = Signal()  # 왼쪽 버튼을 누를 때 (전체 화면에서 제목 표시줄을 잠깐 띄우는 데 쓴다)
    rightClicked = Signal()
    wheeled = Signal(int)  # 휠 한 칸 = +1(위) / -1(아래)

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
            self.pressed.emit()
            self._drag_start = ev.globalPosition().toPoint()
            # self.pos 는 재생목록 위치(숫자)라서 QWidget.pos() 를 가린다 -> 창 위치는 frameGeometry 로 읽는다
            self._win_start = self.window().frameGeometry().topLeft()
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

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self.wheeled.emit(1 if dy > 0 else -1)
        ev.accept()


_ICON_CACHE = {}


# Lucide 아이콘 (ISC 라이선스, 24x24 선 아이콘). 색은 실행 때 지정한 색으로 칠한다.
_LUCIDE = {
    "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
    "play_f": '<polygon points="6 4 20 12 6 20 6 4"/>',
    "pause_f": '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "stop_f": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "stop": '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    "volume": ('<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/>'
               '<path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>'),
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "square": '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    "minus": '<path d="M5 12h14"/>',
    "folder": ('<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>'),
    "save": ('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>'),
    "open": ('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>'),
    "refresh": ('<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
                '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>'),
}


def draw_icon(kind, color="#2b2622", size=16):
    """Lucide 아이콘을 지정한 색·크기로 그린다 (유니코드 글자/이모지 대신). kind 가 없으면 빈 칸."""
    key = (kind, color, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    body = _LUCIDE.get(kind, "")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
           f'fill="{color if kind.endswith('_f') else 'none'}" stroke="{'none' if kind.endswith('_f') else color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    if body:
        p = QPainter(pm)
        QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, size, size))
        p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def icon_btn(btn, kind):
    """버튼에 아이콘을 붙이고 글자는 옆에 그대로 둔다."""
    btn.setIcon(draw_icon(kind))
    btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    btn.setIconSize(QSize(16, 16))


def icon_only(btn, kind, tip):
    """아이콘만 보이게 (글자는 마우스를 올리면 말풍선으로)."""
    btn.setIcon(draw_icon(kind, size=20))
    btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
    btn.setIconSize(QSize(20, 20))
    btn.setFixedSize(30, 30)
    btn.setStyleSheet("QToolButton { padding: 0px; background: #f3ecdd; border: 1px solid #e2d8c6; border-radius: 8px; }"
                      "QToolButton:hover { background: #ebe2d0; }")  # 시안과 같은 테두리 칸 (글자 여백은 0)
    btn.setToolTip(tip)


def _extra_qss():
    """콤보 상자 화살표(구분선이 있는 칸)와 둥근 재생·정지 버튼 모양 - 시안과 같게."""
    path = os.path.join(tempfile.gettempdir(), "jyplayer_chevron_dark.svg")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
                    'stroke="#2b2622" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>')
        url = path.replace("\\", "/")
    except OSError:
        url = ""
    return (
        "QComboBox { min-height: 30px; padding: 0 30px 0 10px; border-radius: 8px; }\n"
        "QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 26px; border: none; "
        "background: transparent; outline: none; margin: 0px; padding: 0px; }\n"
        "QComboBox:focus, QComboBox:on { outline: none; }\n"
        f"QComboBox::down-arrow {{ image: url({url}); width: 14px; height: 14px; }}\n"
        "QComboBox:on, QComboBox:focus, QComboBox:hover { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; "
        "border-radius: 8px; padding: 0 30px 0 10px; }\n"
        "QComboBox::drop-down:on { background: transparent; border: none; }\n"
        "QToolButton#primary { border-radius: 20px; padding: 0px; }\n"
        "QToolButton#round { border-radius: 20px; padding: 0px; }\n"
        "QToolButton#outline { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; border-radius: 8px; font-weight: 600; padding: 6px 14px; }\n"
        "QWidget#titlebar { background: #f3ecdd; border-bottom: 1px solid #e2d8c6; }\n"
        "QLabel#wintitle { color: #2b2622; font-size: 12px; padding-left: 2px; }\n"
        "QToolButton#winbtn, QToolButton#wincls { background: transparent; border: none; border-radius: 0px; padding: 0px; }\n"
        "QToolButton#winbtn:hover { background: #ebe2d0; }\n"
        "QToolButton#wincls:hover { background: #c42b1c; }\n"
        "QSplitter::handle { background: #e2d8c6; }\n"
        "QWidget#leftpane { background: #f3ecdd; }\n"
        "QWidget#side { background: #f3ecdd; }\n"
    )


class MiddleElideDelegate(QStyledItemDelegate):
    """긴 파일명은 가운데를 줄인다 (앞·뒤를 남김): hhd800.com@PRED…restored.mp4"""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideMiddle


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


class VolumeSlider(QSlider):
    """클릭/드래그한 위치로 바로 볼륨이 바뀌는 슬라이더 (휠로도 조절)"""
    volumeRequested = Signal(int)

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setRange(0, VOLUME_MAX)
        self.setFixedWidth(90)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip("볼륨")
        self._drag = False

    def _vol(self, ev):
        return round(min(1.0, max(0.0, ev.position().x() / max(1, self.width()))) * VOLUME_MAX)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = True
            self.volumeRequested.emit(self._vol(ev))

    def mouseMoveEvent(self, ev):
        if self._drag:
            self.volumeRequested.emit(self._vol(ev))

    def mouseReleaseEvent(self, ev):
        self._drag = False

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self.volumeRequested.emit(max(0, min(VOLUME_MAX, self.value() + (VOLUME_STEP if dy > 0 else -VOLUME_STEP))))
        ev.accept()


class FullscreenBar(QWidget):
    """전체화면에서 마우스를 화면 아래로 가져가면 뜨는 재생바 (mpv 창 위에 올리려고 별도의 맨 위 창으로 만든다)."""
    HEIGHT = 46

    def __init__(self, parent):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("fsbar")
        self.btn_play = QToolButton()
        self.btn_play.setIcon(draw_icon("play_f", "#ffffff"))
        self.btn_play.setFocusPolicy(Qt.NoFocus)
        self.slider = ClickSlider()
        self.lbl_time = QLabel("--:-- / --:--")
        self.lbl_vol = QLabel()
        self.lbl_vol.setPixmap(draw_icon("volume").pixmap(16, 16))
        self.vol_slider = VolumeSlider()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.addWidget(self.btn_play)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.lbl_time)
        lay.addWidget(self.lbl_vol)
        lay.addWidget(self.vol_slider)
        self.setStyleSheet("""
            QWidget#fsbar { background: #f3ecdd; border-top: 1px solid #e2d8c6; }
            QLabel { color: #2b2622; }
            QToolButton { color: #2b2622; background: #f3ecdd; border: 1px solid #e2d8c6; border-radius: 4px; padding: 6px 12px; }
            QToolButton:hover { background: #ebe2d0; }
            QToolButton#primary { background: #2f3237; border-color: #2f3237; color: #ffffff; }
            QSlider::groove:horizontal { height: 4px; background: #e2d8c6; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #2f3237; border-radius: 2px; }
            QSlider::handle:horizontal { width: 12px; margin: -5px 0; background: #2b2622; border-radius: 6px; }
        """)
        self.btn_play.setObjectName("primary")
        self.setWindowOpacity(0.9)

    def place_on(self, screen_geo):
        self.setGeometry(screen_geo.x(), screen_geo.y() + screen_geo.height() - self.HEIGHT,
                         screen_geo.width(), self.HEIGHT)


class SwitchCheck(QCheckBox):
    """랜덤 스위치: 시안처럼 둥근 토글 + 글자 (체크박스와 같은 신호·값을 쓴다)."""

    def sizeHint(self):
        return QSize(44 + self.fontMetrics().horizontalAdvance(self.text()) + 6, 22)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2f3237" if on else "#e2d8c6"))
        p.drawRoundedRect(QRectF(0, 3, 36, 18), 9, 9)
        p.setBrush(QColor("#ffffff") if on else QColor("#7a6f63"))
        p.drawEllipse(QRectF(20 if on else 2, 5, 14, 14))
        p.setPen(QColor("#2b2622" if on else "#7a6f63"))
        p.setFont(self.font())
        p.drawText(QRectF(44, 0, self.width() - 44, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()


class TitleBar(QWidget):
    """창 틀을 뺀 뒤 우리가 그리는 제목 표시줄: 파일명 · 최소화 · 최대화 · 닫기.
    끌면 창이 움직이고, 더블클릭하면 최대화/원래 크기."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setObjectName("titlebar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(0)
        self.lbl = QLabel(win.windowTitle())
        self.lbl.setObjectName("wintitle")
        win.windowTitleChanged.connect(self.lbl.setText)
        lay.addWidget(self.lbl, 1)
        self.btn_min = self._wbtn("minus", "최소화", "winbtn")
        self.btn_max = self._wbtn("square", "최대화", "winbtn")
        self.btn_close = self._wbtn("x", "닫기", "wincls")
        self.btn_min.clicked.connect(win.showMinimized)
        self.btn_max.clicked.connect(self._toggle_max)
        self.btn_close.clicked.connect(win.close)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)

    def _wbtn(self, kind, tip, name):
        b = QToolButton()
        b.setObjectName(name)
        b.setIcon(draw_icon(kind, size=14))
        b.setIconSize(QSize(14, 14))
        b.setFixedSize(46, 34)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.NoFocus)
        return b

    def _toggle_max(self):
        if self.win.isMaximized():
            self.win.showNormal()
        else:
            self.win.showMaximized()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.win.windowHandle() is not None:
            self.win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._toggle_max()


class AddDialog(QDialog):
    """+ 추가 창. 파일을 고르면 그 파일들을, 아무것도 고르지 않으면 지금 보고 있는 폴더 전체를 넣는다."""

    def __init__(self, parent, start_dir):
        super().__init__(parent)
        self.setWindowTitle("재생목록에 추가")
        self.resize(680, 480)
        self.setStyleSheet((parent.styleSheet() if parent is not None else "") +
                           "QDialog { background: #f3ecdd; } "
                           "QListView { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; border-radius: 6px; } "
                           "QPushButton { color: #2b2622; background: #f3ecdd; border: 1px solid #e2d8c6; border-radius: 6px; padding: 6px 14px; } "
                           "QPushButton:default { background: #2f3237; color: #ffffff; border-color: #2f3237; font-weight: 600; } ")
        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
        self.model.setNameFilters(["*" + e for e in sorted(VIDEO_EXTS)])
        self.model.setNameFilterDisables(False)  # 동영상이 아닌 파일은 숨긴다 (폴더는 그대로 보임)
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.selectionModel().selectionChanged.connect(self._update_ok)
        self.lbl_path = QLabel()
        self.lbl_path.setObjectName("title")
        btn_up = QPushButton("상위 폴더")
        btn_up.clicked.connect(self._go_up)
        self.btn_ok = QPushButton()
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        top = QHBoxLayout()
        top.addWidget(btn_up)
        top.addWidget(self.lbl_path, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(btn_cancel)
        bottom.addWidget(self.btn_ok)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)
        lay.addLayout(bottom)
        self.current = ""
        self._go(start_dir)

    def _go(self, path):
        self.current = os.path.abspath(path)
        idx = self.model.setRootPath(self.current)
        self.view.setRootIndex(idx)
        self.lbl_path.setText(self.current)
        self._update_ok()

    def _on_double_click(self, idx):
        path = self.model.filePath(idx)
        if os.path.isdir(path):
            self._go(path)

    def _go_up(self):
        parent = os.path.dirname(self.current)
        if parent and parent != self.current:
            self._go(parent)

    def _update_ok(self, *_):
        n = len(self.view.selectedIndexes())
        self.btn_ok.setText(f"선택한 {n}개 추가" if n else "이 폴더 추가")

    def chosen_paths(self):
        paths = [self.model.filePath(i) for i in self.view.selectedIndexes()]
        return paths or [self.current]


class MainWindow(QMainWindow):
    _EDGE = 6  # 창 가장자리에서 이만큼 안쪽이면 크기 조절

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint)  # 창 틀을 빼고 아래에서 제목 표시줄을 직접 그린다
        self.setWindowTitle("JY_Player")
        self.resize(1280, 800)
        self.setAcceptDrops(True)

        self.settings = self._load_settings()
        self.rotations = self.settings.setdefault("rotation", {})
        self.random_mode = bool(self.settings.get("random", False))
        self.audio_delays = self.settings.setdefault("audio_delay", {})  # 파일 경로 -> 소리 싱크(초), 파일마다 저장
        self.watched = set(self.settings.get("watched", []))  # 한 번이라도 재생을 시작한 파일 (재생목록에 ✔ 표시)

        self.files = []   # 폴더 내(하위 폴더 포함) 동영상 (자연 정렬)
        self.order = []   # 재생 순서 (랜덤이면 섞인 순서)
        self.root_folder = ""  # 표시용 상대경로 계산 기준
        self.pos = -1
        self._now_playing_item = None  # 선택(selection)과 별개로 "현재 재생 중" 표시용
        self.pending_deletes = []  # [경로, 완전삭제 여부, 시도 횟수]
        self.consecutive_errors = 0
        self.hanja_held = False           # 한국어 키보드에서 오른쪽 Ctrl이 한자키로 잡히는 경우 대비
        self.hanja_release_seen = False
        self.hangul_held = False          # 한국 키보드의 오른쪽 Alt(한/영 키) 누르고 있는지
        self._msg_token = 0
        self._was_maximized = False
        self._last_cursor_pos = None
        self._cursor_idle_ticks = 0

        self._build_ui()
        self.fs_bar = FullscreenBar(self)
        self.fs_bar.btn_play.clicked.connect(self.toggle_pause)
        self.fs_bar.slider.seekRequested.connect(self._seek_fraction)
        self.fs_bar.vol_slider.volumeRequested.connect(self.set_volume)

        self.bridge = MpvBridge()
        self.bridge.eof.connect(self._on_eof)
        self.bridge.load_error.connect(self._on_load_error)
        self.bridge.file_loaded.connect(self._on_file_loaded)
        self.update_bridge = UpdateBridge()
        self.update_bridge.found.connect(self._on_update_found)

        self.player = mpv.MPV(
            wid=str(int(self.video.winId())),
            vo="gpu",
            hwdec=self.settings.get("hwdec", "auto-safe"),
            cache="yes",
            demuxer_max_bytes=f"{self.settings.get('readahead_mb', 2048)}MiB",
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
        self.video.pressed.connect(self._fs_reveal_chrome)
        self.video.rightClicked.connect(self._show_file_info)
        self.video.wheeled.connect(lambda d: self.change_volume(d * VOLUME_STEP))

        self.btn_play = QToolButton()
        self.btn_play.setObjectName("primary")
        self.btn_play.setIcon(draw_icon("play_f", "#ffffff"))
        self.btn_play.setFixedSize(40, 40)
        self.btn_play.setIconSize(QSize(20, 20))
        self.btn_play.setFocusPolicy(Qt.NoFocus)
        self.btn_play.setToolTip("재생/일시정지")
        self.btn_play.clicked.connect(self.toggle_pause)
        self.btn_stop = QToolButton()
        self.btn_stop.setIcon(draw_icon("stop_f", "#2b2622"))
        self.btn_stop.setObjectName("round")
        self.btn_stop.setFixedSize(40, 40)
        self.btn_stop.setIconSize(QSize(16, 16))
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.setToolTip("정지")
        self.btn_stop.clicked.connect(self.stop_playback)
        self.slider = ClickSlider()
        self.slider.seekRequested.connect(self._seek_fraction)
        self.lbl_time = QLabel("--:-- / --:--")
        self.lbl_time.setObjectName("time")
        self.lbl_vol = QLabel()
        self.lbl_vol.setPixmap(draw_icon("volume").pixmap(16, 16))
        self.vol_slider = VolumeSlider()
        self.vol_slider.volumeRequested.connect(self.set_volume)

        self.lbl_name = QLabel("폴더를 열거나 이 창에 끌어다 놓으세요")
        self.lbl_name.setObjectName("title")
        self.lbl_name.setMinimumWidth(0)
        self.lbl_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # 줄어들 수 있게 (창이 좁을 때 잘림)
        self.lbl_mode = QLabel("")
        self.lbl_mode.setObjectName("mode")
        self.lbl_msg = QLabel("")
        self.lbl_msg.setObjectName("msg")
        self.lbl_index = QLabel("0 / 0")
        self.chk_random = SwitchCheck("랜덤")
        self.chk_random.setFocusPolicy(Qt.NoFocus)
        self.chk_random.toggled.connect(self._on_random_checkbox)
        self.btn_open = QToolButton()
        self.btn_open.setText("폴더 열기")
        self.btn_open.setFocusPolicy(Qt.NoFocus)
        self.btn_open.clicked.connect(self.choose_folder)
        icon_only(self.btn_open, "folder", "폴더 열기")
        self.btn_update = QToolButton()
        self.btn_update.setText("업데이트 확인")
        self.btn_update.setFocusPolicy(Qt.NoFocus)
        self.btn_update.clicked.connect(self.on_update_clicked)
        self.btn_update.setObjectName("outline")  # 시안: 테두리만 있는 강조 버튼

        row1 = QHBoxLayout()
        row1.addWidget(self.btn_play)
        row1.addWidget(self.btn_stop)
        row1.addWidget(self.slider, 1)
        row1.addWidget(self.lbl_time)
        row1.addWidget(self.lbl_vol)
        row1.addWidget(self.vol_slider)
        row2 = QHBoxLayout()
        row2.addWidget(self.lbl_index)
        row2.addWidget(self.lbl_mode)
        row2.addStretch(1)
        row2.addWidget(self.chk_random)
        self.cmb_hwdec = QComboBox()
        self.cmb_hwdec.setFocusPolicy(Qt.NoFocus)
        self.cmb_hwdec.setToolTip("하드웨어 디코딩 (느리면 Direct3D 11 이나 끔을 시험)")
        for label, val in HWDEC_CHOICES:
            self.cmb_hwdec.addItem(label, val)
        self.cmb_hwdec.setCurrentIndex(max(0, [v for _, v in HWDEC_CHOICES].index(self.settings.get("hwdec", "auto-safe"))
                                       if self.settings.get("hwdec", "auto-safe") in [v for _, v in HWDEC_CHOICES] else 0))
        self.cmb_hwdec.currentIndexChanged.connect(self._on_hwdec_changed)
        cap_hw = QLabel("디코딩")
        cap_hw.setObjectName("caption")
        row2.addWidget(cap_hw)
        row2.addWidget(self.cmb_hwdec)
        self.cmb_cache = QComboBox()
        self.cmb_cache.setFocusPolicy(Qt.NoFocus)
        self.cmb_cache.setToolTip("미리 읽기: 디스크에서 미리 가져다 두는 양. 느린 디스크에서 키우면 재생이 부드러워짐")
        for label, mb in READAHEAD_CHOICES:
            self.cmb_cache.addItem(label, mb)
        cur_mb = self.settings.get("readahead_mb", 2048)
        self.cmb_cache.setCurrentIndex(max(0, next((i for i, (_, mb) in enumerate(READAHEAD_CHOICES) if mb == cur_mb), 0)))
        self.cmb_cache.currentIndexChanged.connect(self._on_cache_changed)
        cap_cache = QLabel("미리 읽기")
        cap_cache.setObjectName("caption")
        row2.addWidget(cap_cache)
        row2.addWidget(self.cmb_cache)
        row2.addWidget(self.btn_update)

        self.bar = QWidget()
        self.bar.setObjectName("bar")
        self.bar.setAttribute(Qt.WA_StyledBackground, True)
        bar_layout = QVBoxLayout(self.bar)
        bar_layout.setContentsMargins(14, 10, 14, 10)
        bar_layout.setSpacing(8)
        bar_layout.addLayout(row1)
        bar_layout.addLayout(row2)

        left = QWidget()
        left.setObjectName("leftpane")
        left.setAttribute(Qt.WA_StyledBackground, True)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(self.video, 1)
        left_layout.addWidget(self.bar)

        self.listw = PlaylistWidget()
        self.listw.setFocusPolicy(Qt.NoFocus)
        self.listw.itemDoubleClicked.connect(self._on_list_double_click)
        self.listw.filesDropped.connect(self.add_to_playlist)
        self.listw.setContextMenuPolicy(Qt.CustomContextMenu)
        # 긴 파일명: 가로 스크롤 없이 가운데를 줄인다
        self.listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listw.setItemDelegate(MiddleElideDelegate(self.listw))

        def mkbtn(text, slot, icon_kind):
            b = QToolButton()
            b.setText(text)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(slot)
            icon_only(b, icon_kind, text)
            return b

        # 추가 버튼 하나 - 누르면 파일 또는 폴더를 고르는 메뉴가 뜬다
        self.btn_add = QToolButton()
        self.btn_add.setText("추가")
        self.btn_add.setFocusPolicy(Qt.NoFocus)
        self.btn_add.clicked.connect(self._open_add_dialog)
        icon_only(self.btn_add, "plus", "추가 (파일 또는 폴더)")
        self.btn_remove = mkbtn("제거", self.remove_selected_from_playlist, "minus")
        self.btn_save_list = mkbtn("목록 저장", self.save_playlist, "save")
        self.btn_load_list = mkbtn("목록 열기", self.load_playlist, "open")
        self.lbl_list_count = QLabel("재생목록 · 0개")
        self.lbl_list_count.setObjectName("listhead")

        # 한 줄: 재생목록 개수 + 아이콘 버튼 (추가, 폴더 열기, 제거, 목록 저장, 목록 열기)
        list_tools = QHBoxLayout()
        list_tools.setSpacing(4)
        list_tools.addWidget(self.lbl_list_count)
        list_tools.addStretch(1)
        list_tools.addWidget(self.btn_add)
        list_tools.addWidget(self.btn_open)
        list_tools.addWidget(self.btn_remove)
        list_tools.addWidget(self.btn_save_list)
        list_tools.addWidget(self.btn_load_list)

        self.right_panel = QWidget()
        self.right_panel.setObjectName("side")
        self.right_panel.setAttribute(Qt.WA_StyledBackground, True)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)
        right_layout.addLayout(list_tools)
        right_layout.addWidget(self.listw, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(0)  # 가운데 세로 경계선 없앰
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([980, 300])
        self.titlebar = TitleBar(self)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        body_lay.addWidget(self.titlebar)
        body_lay.addWidget(self.splitter, 1)
        self.setCentralWidget(body)

        self.setStyleSheet("""
            /* 색 토큰 (시안 기준): 배경 #f3ecdd · 패널 #f3ecdd · 면 #f3ecdd · 선 #e2d8c6 · 글자 #2b2622 · 흐린 글자 #7a6f63 · 강조 #2f3237 */
            QMainWindow, QSplitter { background: #f3ecdd; }
            QWidget#bar { background: #f3ecdd; border-top: 1px solid #e2d8c6; }
            QWidget#titlestrip { background: #f3ecdd; }
            QLabel#title { color: #2b2622; font-size: 13px; font-weight: 600; }
            QLabel, QCheckBox { color: #2b2622; background: transparent; }
            QLabel#time { color: #7a6f63; }
            QLabel#listhead { color: #7a6f63; font-size: 12px; padding: 0 2px; }
            QLabel#caption { color: #7a6f63; }
            QLabel#hint { color: #7a6f63; font-size: 11px; padding: 10px 12px; border-top: 1px solid #e2d8c6; }
            QLabel#mode { color: #8a5a00; padding: 0 8px; }
            QLabel#msg { color: #2f3237; padding: 0 8px; }
            QToolTip { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; padding: 4px; }
            QMessageBox { background: #f2f2f2; }
            QMessageBox QLabel { color: #000000; }
            QMessageBox QPushButton { color: #000000; background: #e4e4e4; border: 1px solid #9a9a9a; padding: 4px 14px; min-width: 64px; }
            QListWidget { background: #f3ecdd; color: #7a6f63; border: none; outline: 0; }
            QListWidget::item { padding: 7px 10px; }
            QListWidget::item:hover { background: #f3ecdd; }
            QListWidget::item:selected { background: #e2d8c6; color: #2b2622; }
            QScrollBar:vertical { background: #f3ecdd; width: 12px; margin: 0; }
            QScrollBar::handle:vertical { background: #cfc4b0; min-height: 24px; border-radius: 6px; }
            QScrollBar::handle:vertical:hover { background: #a89d8c; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QToolButton { color: #2b2622; background: #f3ecdd; border: 1px solid #e2d8c6; border-radius: 4px; padding: 6px 12px; }
            QToolButton:hover { background: #ebe2d0; }
            QToolButton:pressed { background: #f3ecdd; }
            QToolButton#primary { background: #2f3237; border-color: #2f3237; color: #ffffff; }
            QToolButton#primary:hover { background: #454a52; }
            QComboBox { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; border-radius: 4px; padding: 3px 8px; min-height: 18px; }
            QComboBox:hover { border-color: #a89d8c; }
            QComboBox QAbstractItemView { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; selection-background-color: #2f3237; }
            QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #a89d8c; border-radius: 3px; background: #f3ecdd; }
            QCheckBox::indicator:checked { background: #2f3237; border-color: #2f3237; }
            QMenu { background: #f3ecdd; color: #2b2622; border: 1px solid #e2d8c6; padding: 4px; }
            QMenu::item { padding: 6px 18px; }
            QMenu::item:selected { background: #2f3237; color: #ffffff; }
            QSlider::groove:horizontal { height: 4px; background: #e2d8c6; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #2f3237; border-radius: 2px; }
            QSlider::handle:horizontal { width: 12px; margin: -5px 0; background: #2b2622; border-radius: 6px; }
        """)

        self.setStyleSheet(self.styleSheet() + _extra_qss())

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
            self.listw.addItem(self._make_item(p))

    def _item_text(self, path):
        return self._display_name(path)

    def _watch_icon(self, path):
        """'본 파일' 표시 - 글자(✔) 대신 그린 체크 아이콘. 안 본 파일도 같은 칸을 비워 두어 글자 위치를 맞춘다."""
        return draw_icon("check", "#7a6f63") if path in self.watched else draw_icon("none")

    def _make_item(self, path):
        item = QListWidgetItem(self._item_text(path))
        item.setIcon(self._watch_icon(path))
        item.setData(Qt.UserRole, path)
        item.setToolTip(path)
        return item

    def _set_watched(self, paths, value):
        changed = False
        for p in paths:
            if (p in self.watched) != value:
                changed = True
                if value:
                    self.watched.add(p)
                else:
                    self.watched.discard(p)
                if p in self.files:
                    it = self.listw.item(self.files.index(p))
                    it.setText(self._item_text(p))
                    it.setIcon(self._watch_icon(p))
        if changed:
            self.settings["watched"] = sorted(self.watched)
            self._save_settings()

    def _show_list_menu(self, point):
        sel = self._selected_paths()
        m = QMenu(self)
        a_on = m.addAction("선택 항목 '본 파일' 표시")
        a_off = m.addAction("선택 항목 표시 해제")
        m.addSeparator()
        a_all = m.addAction("이 목록의 표시 전부 해제")
        a_on.setEnabled(bool(sel))
        a_off.setEnabled(bool(sel))
        act = m.exec(self.listw.viewport().mapToGlobal(point))
        if act is a_on:
            self._set_watched(sel, True)
        elif act is a_off:
            self._set_watched(sel, False)
        elif act is a_all:
            self._set_watched(list(self.files), False)

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
            item.setForeground(QColor("#2f3237"))

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
        self.player.audio_delay = self.audio_delays.get(path, 0.0)  # 이 파일의 소리 싱크 복원
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

    def _on_cache_changed(self, _index):
        """미리 읽기 양 변경: 바로 적용하고 설정(readahead_mb)에 저장."""
        mb = self.cmb_cache.currentData()
        self.settings["readahead_mb"] = mb
        if hasattr(self, "player"):
            self.player["demuxer-max-bytes"] = f"{mb}MiB"
        self.volume_save_timer.start()
        self.flash(f"미리 읽기: {self.cmb_cache.currentText()}")

    def _on_hwdec_changed(self, _index):
        """하드웨어 디코딩 방식 변경: 바로 적용하고 설정에 저장 (다음에 켤 때도 같은 방식)."""
        val = self.cmb_hwdec.currentData()
        self.settings["hwdec"] = val
        if hasattr(self, "player"):
            self.player.hwdec = val
        self.volume_save_timer.start()
        self.flash(f"디코딩: {self.cmb_hwdec.currentText()}")

    def _on_random_checkbox(self, _checked):
        self.toggle_random()

    # ---------------------------------------------------------------- 재생목록 편집
    def _open_add_dialog(self):
        """+ 버튼: 우리 창에서 파일을 고르면 파일을, 아무것도 안 고르면 지금 보는 폴더를 재생목록에 넣는다."""
        start_dir = self.root_folder or self.settings.get("last_folder", "")
        if not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")
        dlg = AddDialog(self, start_dir)
        if dlg.exec() == QDialog.Accepted:
            self.add_to_playlist(dlg.chosen_paths())

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
            self.listw.addItem(self._make_item(p))
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
    def _is_playing(self):
        """재생 중(일시정지 포함)이면 True, 정지 상태(아무것도 안 올라간 상태)면 False."""
        return bool(self.current) and bool(self._mpv_get("path"))

    def delete_selected_or_current(self, permanent):
        """Del 키 우선순위: ①다중 선택 ②지금 재생 중인 영상 ③정지 중이면 클릭(단일 선택)한 항목."""
        selected = self._selected_paths()
        if len(selected) >= 2:
            paths = selected
        elif self._is_playing():
            paths = [self.current]
        else:
            paths = selected[:1]
        if not paths:
            self.flash("지울 항목을 먼저 선택하세요")
            return
        self.delete_paths(paths, permanent)

    def delete_current(self, permanent):
        if self.current:
            self.delete_paths([self.current], permanent)

    def delete_paths(self, paths, permanent):
        if not paths:
            return
        gone = set(paths)
        removed_current = self.current in gone
        # 1) 목록에서 먼저 빼고 (현재 영상이 지워지면) 다음 영상으로 전환 (재생 중 파일 잠금 회피)
        rows = sorted((i for i, p in enumerate(self.files) if p in gone), reverse=True)
        for row in rows:
            item = self.listw.takeItem(row)
            if item is self._now_playing_item:
                self._now_playing_item = None
        self.files = [p for p in self.files if p not in gone]
        kept_before = sum(1 for p in self.order[:max(self.pos, 0)] if p not in gone)
        cur = self.current
        self.order = [p for p in self.order if p not in gone]
        changed = False
        for p in paths:
            if self.rotations.pop(p, None) is not None:
                changed = True
        if changed:
            self._save_settings()

        if not self.order:
            self.pos = -1
            self.player.command("stop")
        elif removed_current:
            self.pos = kept_before
            if self.pos >= len(self.order):
                if self.random_mode:
                    self.order = self.files[:]
                    random.shuffle(self.order)
                    self.pos = 0
                else:
                    self.pos = len(self.order) - 1
            self.play_current()
        else:
            self.pos = self.order.index(cur)
        self._update_labels()

        # 2) 잠금이 풀릴 때까지 재시도하며 삭제
        for p in paths:
            self.pending_deletes.append([p, permanent, 0])
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
    def _codec_label(self):
        """지금 영상의 코덱 이름 (HEVC, H264 …). 아직 모르면 빈 문자열."""
        fmt = (self._mpv_get("video-format") or "").lower()
        names = {"h264": "H264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2"}
        return names.get(fmt, fmt.upper())

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
            amount = f"{abs(seconds)}초"  # 60초도 1분이 아니라 60초로 표시
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
            codec = self._codec_label()
            self.player.show_text(head + name + (f"  [{codec}]" if codec else ""), "1000")

    def _seek_fraction(self, frac, exact):
        if self.current:
            self.player.seek(frac * 100, "absolute-percent", "exact" if exact else "keyframes")

    def change_volume(self, delta):
        self.set_volume(round(self.player.volume) + delta)

    def set_volume(self, vol):
        vol = max(0, min(VOLUME_MAX, int(vol)))
        self.player.volume = vol
        self.settings["volume"] = vol
        self.volume_save_timer.start()  # 연타/길게 누름 중 매번 디스크에 쓰지 않도록 묶어서 저장
        self.player.show_text(f"볼륨 {vol}%", "800")
        self._update_mode_label()

    def adjust_audio_delay(self, delta):
        """. 는 소리를 늦추고 , 는 앞당긴다 (0.1초 단위). 파일마다 값을 저장해서 다시 열어도 유지된다."""
        if not self.current:
            return
        new = round(self.audio_delays.get(self.current, 0.0) + delta, 3)
        self.player.audio_delay = new
        if abs(new) < 1e-6:
            self.audio_delays.pop(self.current, None)  # 0이면 기록을 지움
        else:
            self.audio_delays[self.current] = new
        self.volume_save_timer.start()  # 연타해도 디스크에는 잠깐 뒤에 한 번만 씀
        self._update_mode_label()
        self.player.show_text(f"소리 싱크 {new:+.1f}초", "1000")

    def change_zoom(self, delta):
        """delta=None 이면 원래 크기로."""
        if not self.current:
            return
        zoom = 0.0 if delta is None else max(-2.0, min(3.0, (self._mpv_get("video_zoom") or 0.0) + delta))
        self.player["video-zoom"] = zoom
        if abs(zoom) < 0.005:
            self.player["video-pan-x"] = 0
            self.player["video-pan-y"] = 0
        self._clamp_pan()
        self.player.show_text(f"확대 {2 ** zoom * 100:.0f}%", "800")

    def _pan_limit(self):
        """확대한 뒤 화면 가장자리에 검은 여백이 보이지 않도록 허용되는 이동 범위(화면 크기 대비)."""
        return max(0.0, (2 ** (self._mpv_get("video_zoom") or 0.0) - 1) / 2)

    def _clamp_pan(self, dx=0.0, dy=0.0):
        limit = self._pan_limit()
        x = (self._mpv_get("video_pan_x") or 0.0) + dx
        y = (self._mpv_get("video_pan_y") or 0.0) + dy
        self.player["video-pan-x"] = max(-limit, min(limit, x))
        self.player["video-pan-y"] = max(-limit, min(limit, y))

    def pan_video(self, dx=0.0, dy=0.0):
        if not self.current:
            return
        if self._pan_limit() == 0:
            self.player.show_text("확대한 뒤 숫자패드 2/4/6/8 로 이동", "800")
            return
        self._clamp_pan(dx, dy)

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

    def cycle_subtitle(self):
        """J - 자막 트랙을 끔 -> 1번 -> 2번 ... -> 끔 순으로 바꾼다."""
        subs = [t for t in (self._mpv_get("track_list") or []) if t.get("type") == "sub"]
        if not self.current or not subs:
            self.player.show_text("자막 없음", "1000")
            return
        ids = [t["id"] for t in subs]
        cur = self._mpv_get("sid")
        nxt = ids[0] if not cur else (ids[ids.index(cur) + 1] if cur in ids and ids.index(cur) + 1 < len(ids) else None)
        if nxt is None:
            self.player["sid"] = "no"
            self.player.show_text("자막 끔", "1000")
        else:
            self.player.sid = nxt
            t = next(x for x in subs if x["id"] == nxt)
            self.player.show_text(f"자막 {ids.index(nxt) + 1}/{len(ids)}  {t.get('lang') or ''} {t.get('title') or t.get('codec') or ''}".strip(), "1200")

    def reset_rotation(self):
        if self.current and self.rotations.get(self.current, 0):
            self.rotate(-self.rotations[self.current])
        else:
            self.player.show_text("회전 0°", "1000")

    def _fs_reveal_chrome(self):
        """전체 화면에서 화면을 누르면 제목 표시줄을 3초 띄웠다가 다시 숨긴다."""
        if not self.isFullScreen():
            return
        self.titlebar.show()
        self._fs_token = getattr(self, "_fs_token", 0) + 1
        token = self._fs_token
        QTimer.singleShot(3000, lambda: token == self._fs_token and self._fs_hide_chrome())

    def _fs_hide_chrome(self):
        if self.isFullScreen():
            self.titlebar.hide()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._was_maximized = self.isMaximized()
            self.bar.hide()
            self.right_panel.hide()
            self.titlebar.hide()  # 전체 화면에서는 제목 표시줄(최소화·최대화·닫기)도 숨긴다
            self.showFullScreen()

    def exit_fullscreen(self):
        if not self.isFullScreen():
            return
        self.fs_bar.hide()
        self.bar.show()
        self.right_panel.show()
        self.titlebar.show()
        self.video.unsetCursor()
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    # ---------------------------------------------------------------- 키 입력
    def _edges_at(self, gp):
        """마우스(전역 좌표)가 창 가장자리에 있으면 그 가장자리 값, 아니면 None."""
        if self.isMaximized() or self.isFullScreen() or not self.frameGeometry().contains(gp):
            return None
        p = self.mapFromGlobal(gp)
        r = self.rect()
        m = self._EDGE
        edges = []
        if p.x() < m:
            edges.append(Qt.LeftEdge)
        elif p.x() > r.width() - m:
            edges.append(Qt.RightEdge)
        if p.y() < m:
            edges.append(Qt.TopEdge)
        elif p.y() > r.height() - m:
            edges.append(Qt.BottomEdge)
        if not edges:
            return None
        result = edges[0]
        for e in edges[1:]:
            result = result | e
        return result

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et in (QEvent.MouseMove, QEvent.MouseButtonPress) and self.windowHandle() is not None:
            edges = self._edges_at(QCursor.pos())
            if et == QEvent.MouseButtonPress and edges is not None and ev.button() == Qt.LeftButton:
                self.windowHandle().startSystemResize(edges)
                return True
            if et == QEvent.MouseMove:
                if edges is None:
                    self.unsetCursor()
                else:
                    horiz = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
                    vert = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
                    if horiz and vert:
                        self.setCursor(Qt.SizeFDiagCursor)
                    elif horiz:
                        self.setCursor(Qt.SizeHorCursor)
                    else:
                        self.setCursor(Qt.SizeVerCursor)
        if et == QEvent.KeyRelease:
            if ev.nativeVirtualKey() == VK_HANJA and not ev.isAutoRepeat():
                self.hanja_held = False
                self.hanja_release_seen = True
            if ev.nativeVirtualKey() == VK_HANGUL and not ev.isAutoRepeat():
                self.hangul_held = False
            return False
        if et == QEvent.WindowDeactivate and obj is self:
            self.hanja_held = False
            self.hangul_held = False
            return False
        if et != QEvent.KeyPress or QApplication.activeModalWidget() is not None:
            return False
        return self.handle_key(ev)

    def handle_key(self, ev):
        vk = ev.nativeVirtualKey()
        if vk == VK_HANJA:
            self.hanja_held = True
            return True
        if vk == VK_HANGUL:  # 오른쪽 Alt(한/영 키) 눌림 - 누르고 있는 동안만 Alt 로 취급
            self.hangul_held = True
            return True
        qmods = ev.modifiers()
        shift = bool(qmods & Qt.ShiftModifier) or key_down(VK_SHIFT)
        # 한자키(=한국어 키보드의 오른쪽 Ctrl)는 눌림 이벤트만 믿으면 안 떼졌을 때 계속 눌린 걸로 남을 수 있어서,
        # 실제 키 상태(GetAsyncKeyState)가 지금 눌려 있을 때만 Ctrl로 취급한다.
        ctrl = (bool(qmods & Qt.ControlModifier) or key_down(VK_CONTROL)
                or (self.hanja_held and async_key_down(VK_HANJA)))

        # Alt = 60초 이동. 왼쪽 Alt, 오른쪽 Alt(VK_RMENU), 한국 키보드의 오른쪽 Alt(한/영 키) 모두 인정
        alt = (bool(qmods & Qt.AltModifier) or key_down(0x12) or key_down(0xA5)
               or (self.hangul_held and async_key_down(VK_HANGUL)))
        if vk in (VK_LEFT, VK_RIGHT):
            step = 60 if alt else SEEK_STEPS[(ctrl, shift)]
            self.seek_relative(step if vk == VK_RIGHT else -step)
        elif vk in (VK_UP, VK_DOWN):
            self.change_volume(VOLUME_STEP if vk == VK_UP else -VOLUME_STEP)
        elif vk == VK_ADD:
            self.change_zoom(ZOOM_STEP)
        elif vk == VK_SUBTRACT:
            self.change_zoom(-ZOOM_STEP)
        elif vk == VK_MULTIPLY:
            self.change_zoom(None)
        elif vk in (VK_NUMPAD2, VK_NUMPAD4, VK_NUMPAD6, VK_NUMPAD8):
            # 8 위 / 2 아래 / 4 왼쪽 / 6 오른쪽 - 화면(영상)이 그 방향으로 움직인다
            dx = {VK_NUMPAD4: -PAN_STEP, VK_NUMPAD6: PAN_STEP}.get(vk, 0.0)
            dy = {VK_NUMPAD8: -PAN_STEP, VK_NUMPAD2: PAN_STEP}.get(vk, 0.0)
            self.pan_video(dx, dy)
        elif vk == VK_PRIOR:
            self.prev_video()
        elif vk == VK_NEXT:
            self.next_video()
        elif vk == VK_DELETE:
            if not ev.isAutoRepeat():
                self.delete_selected_or_current(permanent=shift)
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
        elif vk == VK_J:
            if not ev.isAutoRepeat():
                self.cycle_subtitle()
        elif vk == VK_OEM_PERIOD:
            self.adjust_audio_delay(AUDIO_DELAY_STEP)
        elif vk == VK_OEM_COMMA:
            self.adjust_audio_delay(-AUDIO_DELAY_STEP)
        elif vk == VK_OEM_4:
            if not ev.isAutoRepeat():
                self.reset_rotation()
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
        if self.current:
            self._set_watched([self.current], True)
            try:
                self.player.show_text(os.path.basename(self.current), "3000")  # 처음 열 때 파일명을 3초
            except Exception:
                pass
        # 기본(default) 표시가 없는 자막(캠코더 MTS 의 PGS 등)은 mpv 가 자동으로 안 켜므로 첫 자막을 켠다
        subs = [t for t in (self._mpv_get("track_list") or []) if t.get("type") == "sub"]
        if subs and not self._mpv_get("sid"):
            try:
                self.player.sid = subs[0]["id"]
            except Exception:
                pass
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
            f"디코딩: {self._mpv_get('hwdec_current') or '알 수 없음'} (설정: {self.cmb_hwdec.currentText()})",
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
        self.btn_update.setStyleSheet("QToolButton { color: #ffffff; background: #2f3237; border-color: #2f3237; font-weight: bold; }")
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
        return QMessageBox.question(self, "JY_Player 업데이트", text) == QMessageBox.Yes

    def _warn(self, text):
        QMessageBox.warning(self, "JY_Player 업데이트", text)

    def on_update_clicked(self):
        self.btn_update.setEnabled(False)
        try:
            self._do_update()
        finally:
            self.btn_update.setEnabled(True)

    def _do_update(self):
        cur = jyupdate.installed_version()
        if not cur:
            QMessageBox.information(self, "JY_Player 업데이트", "설치된 JY_Player 에서만 업데이트할 수 있습니다.")
            return
        gh = jyupdate.find_gh()
        if not gh:
            if not self._ask("업데이트에는 GitHub CLI(gh)가 필요합니다.\n지금 설치할까요? (콘솔 창이 열립니다)"):
                return
            self._busy(jyupdate.install_gh_in_console)
            gh = jyupdate.find_gh()
            if not gh:
                self._warn("gh 를 찾지 못했습니다. 설치가 끝났다면 JY_Player 를 다시 켠 뒤 시도하세요.")
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
            QMessageBox.information(self, "JY_Player 업데이트", f"이미 최신 버전입니다. (v{cur})")
            return
        if not self._ask(f"v{cur} → v{latest} 로 업데이트할까요?\n받은 뒤 JY_Player 가 종료되고 다시 시작됩니다."):
            return
        self.flash("업데이트 파일을 받는 중...")
        setup = self._busy(lambda: jyupdate.download_setup(gh, latest, "JYPlayer-Setup-*.exe"))
        if not setup:
            self._warn("업데이트 파일을 받지 못했습니다.")
            return
        jyupdate.launch_setup(setup, "jyplayer")
        self.close()
    # ---------------------------------------------------------------- 표시
    def flash(self, msg, error=False):
        self._msg_token += 1
        token = self._msg_token
        self.lbl_msg.setStyleSheet("color: #b42318;" if error else "")
        self.lbl_msg.setText(msg)
        try:
            self.player.show_text(msg, "3000")
        except Exception:
            pass
        QTimer.singleShot(3000, lambda: token == self._msg_token and self.lbl_msg.setText(""))

    def _update_labels(self):
        path = self.current
        if path:
            name = self._display_name(path)
            self.lbl_name.setText(name)
            self.setWindowTitle(f"{os.path.basename(path)} - JY_Player")
            idx = self.files.index(path) + 1 if path in self.files else 0
            self.lbl_index.setText(f"{idx} / {len(self.files)}")
            self._mark_now_playing(self.listw.item(idx - 1) if idx else None)
            if idx:
                self.listw.scrollToItem(self.listw.item(idx - 1))
        else:
            self.lbl_name.setText("재생 중인 영상 없음")
            self.setWindowTitle("JY_Player")
            self.lbl_index.setText(f"0 / {len(self.files)}")
            self.lbl_time.setText("--:-- / --:--")
            self.slider.setValue(0)
        self.lbl_list_count.setText(f"재생목록 · {len(self.files)}개")
        self._update_mode_label()

    def _update_mode_label(self):
        parts = []
        deg = self.rotations.get(self.current, 0) if self.current else 0
        if deg:
            parts.append(f"회전 {deg}°")
        d = getattr(self, "audio_delays", {}).get(self.current, 0.0) if self.current else 0.0
        if abs(d) > 1e-6:
            parts.append(f"소리 싱크 {d:+.1f}초")
        vol = round(self.player.volume) if hasattr(self, "player") else 100
        for sl in (self.vol_slider, self.fs_bar.vol_slider):
            if sl.value() != vol:
                sl.setValue(vol)
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
            self.fs_bar.lbl_time.setText(self.lbl_time.text())
            # 버튼은 "누르면 일어날 동작"을 보여줌: 재생 중엔 일시정지 아이콘, 멈춰 있으면 재생 아이콘
            self.btn_play.setIcon(draw_icon("play_f" if (t is None or paused) else "pause_f", "#ffffff"))
            self.fs_bar.btn_play.setIcon(self.btn_play.icon())
            if d and t is not None:
                for sl in (self.slider, self.fs_bar.slider):
                    if not sl.dragging:
                        sl.setValue(int(t / d * 1000))
        else:
            self.btn_play.setIcon(draw_icon("play_f", "#ffffff"))
            self.fs_bar.btn_play.setIcon(draw_icon("play_f", "#ffffff"))

        # 전체화면에서 마우스가 2초간 멈추면 커서 숨김
        if self.isFullScreen():
            # 영상 창(mpv)은 별도 창이라 클릭 신호가 안 올 수 있어서, 버튼이 눌린 순간을 직접 확인한다
            buttons_down = bool(QApplication.mouseButtons() & (Qt.LeftButton | Qt.RightButton))
            if buttons_down and not getattr(self, "_mouse_was_down", False):
                self._fs_reveal_chrome()
            self._mouse_was_down = buttons_down
            pos = QCursor.pos()
            geo = self.screen().geometry()
            # 마우스를 화면 아래쪽으로 가져가면 재생바가 뜨고, 위로 올리면 사라진다 (드래그 중에는 유지)
            zone = FullscreenBar.HEIGHT + (30 if self.fs_bar.isVisible() else 0)
            near_bottom = geo.contains(pos) and pos.y() >= geo.y() + geo.height() - zone
            if near_bottom or self.fs_bar.slider.dragging or self.fs_bar.vol_slider._drag:
                if not self.fs_bar.isVisible():
                    self.fs_bar.place_on(geo)
                    self.fs_bar.show()
                self._cursor_idle_ticks = 0
                self.video.unsetCursor()
            elif self.fs_bar.isVisible():
                self.fs_bar.hide()
            if pos != self._last_cursor_pos:
                self._last_cursor_pos = pos
                self._cursor_idle_ticks = 0
                self.video.unsetCursor()
            else:
                self._cursor_idle_ticks += 1
                if self._cursor_idle_ticks == 10 and not self.fs_bar.isVisible():
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
    app.setStyle("Fusion")  # 위젯 기본 모양이 OS마다 달라서, 스타일시트 색이 어디서나 같게 나오도록
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

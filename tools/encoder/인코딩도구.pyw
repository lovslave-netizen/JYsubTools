"""H.265 인코딩 도구 - 폴더 스캔(하위폴더 포함) → 코덱/비트레이트 확인 → 선택해서 인코딩.

인코더: CPU(libx265) / N150(Intel QSV) / 9070XT(AMD AMF)
해상도·오디오·자막은 원본 그대로 복사하고 영상 코덱만 H.265로 바꾼다.
ffmpeg/ffprobe는 bin 폴더(빌드 시 exe에 번들)에서 찾는다.
"""
import json
import os
import subprocess
import sys
import threading
import time
import datetime
import queue
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts",
             ".mts", ".flv", ".webm", ".mpg", ".mpeg", ".vob", ".3gp"}
PREFIX = "h265_"  # 결과 파일명 앞에 붙임 (이름순 정렬 시 한데 모임)
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 인코더: 키 -> (표시명, 기본 품질값)
ENCODERS = {
    "cpu": ("CPU  (libx265)", 23),
    "qsv": ("N150  (Intel QSV)", 24),
    "amf": ("9070XT  (AMD AMF)", 24),
}

SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "인코딩도구.json")


def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def tool(name):
    # 설치형: 설치폴더\bin (공용) -> exe 옆 bin -> (예전 빌드) 번들 bin -> PATH
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(exe_dir, "..", "bin"), os.path.join(exe_dir, "bin"), os.path.join(base_dir(), "bin")):
        p = os.path.normpath(os.path.join(d, name + ".exe"))
        if os.path.exists(p):
            return p
    return name


FFMPEG = tool("ffmpeg")
FFPROBE = tool("ffprobe")


def trash(path):
    """휴지통으로 이동 (SHFileOperationW). 성공 여부 반환."""
    import ctypes
    from ctypes import wintypes

    class OP(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("func", wintypes.UINT), ("src", wintypes.LPCWSTR),
                    ("dst", wintypes.LPCWSTR), ("flags", ctypes.c_ushort),
                    ("aborted", wintypes.BOOL), ("maps", ctypes.c_void_p), ("title", wintypes.LPCWSTR)]

    # FO_DELETE, FOF_ALLOWUNDO|FOF_NOCONFIRMATION|FOF_NOERRORUI|FOF_SILENT
    op = OP(None, 3, os.path.abspath(path) + "\0", None, 0x40 | 0x10 | 0x400 | 0x4, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.aborted


def delete_file(path):
    """휴지통 없이 바로 삭제 (쓸모없는 결과 파일용). 성공 여부 반환."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def copy_times(src, dst):
    """원본의 만든 날짜/수정한 날짜/액세스 날짜를 결과 파일에 복사."""
    import ctypes
    from ctypes import wintypes
    st = os.stat(src)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(dst, 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
    if h == wintypes.HANDLE(-1).value or h is None:
        return

    def ft(ns):  # ns(1970) -> FILETIME(1601, 100ns)
        return ctypes.c_ulonglong(ns // 100 + 116444736000000000)

    c, a, m = ft(st.st_ctime_ns), ft(st.st_atime_ns), ft(st.st_mtime_ns)  # Windows: st_ctime = 만든 날짜
    k32.SetFileTime(h, ctypes.byref(c), ctypes.byref(a), ctypes.byref(m))
    k32.CloseHandle(h)


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fmt_dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m = r // 60
    return f"{h}시간 {m}분" if h else f"{m}분" if m else f"{sec}초"


def pixels(res):
    try:
        w, h = res.split("x")
        return int(w) * int(h)
    except ValueError:
        return 1920 * 1080


def fmt_rate(bps):
    if not bps:
        return "?"
    return f"{bps / 1e6:.2f} Mbps" if bps >= 1e6 else f"{bps / 1e3:.0f} kbps"


def probe(path):
    """ffprobe로 영상 정보. 실패하면 None."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, creationflags=NOWIN, timeout=60)
        j = json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception:
        return None
    v = next((s for s in j.get("streams", [])
              if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not v:
        return None
    fmt = j.get("format", {})
    has_audio = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    w, h = v.get("width"), v.get("height")
    rot = 0
    try:
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        rot = rot or int(float(v.get("tags", {}).get("rotate", 0) or 0))
    except (TypeError, ValueError):
        rot = 0
    if w and h and rot % 180 == 90:  # 폰 영상: 화면에 보이는 방향 기준으로 표시/합치기
        w, h = h, w
    fps = v.get("avg_frame_rate") or ""
    if not fps or fps.startswith("0/"):
        fps = v.get("r_frame_rate") or "30"
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    size = int(fmt.get("size") or os.path.getsize(path))
    br = int(v.get("bit_rate") or 0)
    if not br and dur:  # 컨테이너에 영상 비트레이트가 없으면 전체 비트레이트로 대체
        br = int(fmt.get("bit_rate") or size * 8 / dur)
    return {
        "codec": v.get("codec_name", "?"),
        "bitrate": br,
        "res": f"{w or '?'}x{h or '?'}",
        "audio": has_audio,
        "fps": fps,
        "tenbit": "10" in (v.get("pix_fmt") or ""),
        "duration": dur,
        "size": size,
    }


# 정렬 옵션: 표시명 -> (키 함수(item), 내림차순 여부). 이름이 같으면 안정 정렬이라 이전 순서 유지
SORTS = {
    "이름순": (lambda it: it["label"].lower(), False),
    "코덱순 (H.265 마지막)": (lambda it: (it["info"]["codec"] == "hevc", it["info"]["codec"],
                                        it["label"].lower()), False),
    "인코딩 안 한 것 먼저": (lambda it: (bool(it.get("out")), it["info"]["codec"] == "hevc",
                                    it["label"].lower()), False),
    "비트레이트 높은순": (lambda it: it["info"]["bitrate"], True),
    "크기 큰순": (lambda it: it["info"]["size"], True),
    "감소율 큰순": (lambda it: (1 - out_view(it)[0] / out_view(it)[1]) if out_view(it) else -9, True),
}
AFTER = ("아무것도 안 함", "시스템 종료")
PRESETS = ("slow", "medium", "fast")
AMF_QUALITY = {"slow": "quality", "medium": "balanced", "fast": "speed"}


CACHE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "인코딩도구_cache.json")


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def cached_probe(path, cache):
    """파일 크기+수정시각이 같으면 저장해 둔 ffprobe 결과를 재사용. (info, 캐시적중 0/1)
    존재 여부와 결과 파일 연결은 항상 실제 파일시스템으로 확인하므로 수동 삭제와 충돌하지 않는다."""
    try:
        st = os.stat(path)
    except OSError:
        return None, 0
    key = os.path.normpath(path)
    c = cache.get(key)
    if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime and "audio" in c["info"]:
        return c["info"], 1
    info = probe(path)
    if info:
        cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "info": info}
    return info, 0


def out_view(it):
    """(결과 크기, 비교할 원본 크기). 일반 결과는 그 파일 기준, 합친 결과는 묶음 원본 합계 기준. 없으면 None."""
    if it.get("out"):
        return it["out"]["size"], it["info"]["size"]
    if it.get("mout"):
        return it["mout"]["size"], it["mout"]["orig_size"]
    return None


def build_cmd(src, dst, enc, q, preset, info):
    ext = os.path.splitext(dst)[1].lower()
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-y", "-progress", "pipe:1", "-i", src]
    if ext == ".mkv":
        cmd += ["-map", "0", "-c", "copy"]
    else:  # mp4 등: 영상/오디오/자막(mov_text)만
        cmd += ["-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-c:a", "copy", "-c:s", "mov_text"]
    cmd += ["-map_metadata", "0"]
    cmd += enc_args(enc, q, preset, info["tenbit"])
    cmd += ["-tag:v", "hvc1", dst]
    return cmd


def enc_args(enc, q, preset, ten):
    if enc == "cpu":
        return ["-c:v", "libx265", "-crf", str(q), "-preset", preset,
                "-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
    if enc == "qsv":
        return ["-c:v", "hevc_qsv", "-global_quality", str(q), "-preset", preset,
                "-pix_fmt", "p010le" if ten else "nv12"]
    return ["-c:v", "hevc_amf", "-quality", AMF_QUALITY[preset], "-rc", "cqp",
            "-qp_i", str(q), "-qp_p", str(q + 2),
            "-pix_fmt", "p010le" if ten else "nv12"]


def build_merge_cmd(items, dst, enc, q, preset):
    """items = [(경로, info), ...] 를 순서대로 이어 붙이면서 H.265 로 인코딩 (concat 필터 - 해상도/코덱이 달라도 됨).
    해상도는 첫 영상 기준(다르면 비율 유지 + 검은 여백), 음성이 없는 영상은 무음으로 채우고, 자막/챕터는 버린다."""
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-y", "-progress", "pipe:1"]
    for path, _ in items:
        cmd += ["-i", path]
    first = items[0][1]
    fps = first.get("fps") or "30"
    same_res = len({i["res"] for _, i in items}) == 1
    try:
        w, h = (int(x) for x in first["res"].split("x"))
    except ValueError:
        w = h = 0
        same_res = True
    any_audio = any(i.get("audio", True) for _, i in items)
    parts, labels = [], ""
    for n, (_, info) in enumerate(items):
        vf = f"[{n}:v:0]"
        if not same_res:
            vf += f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
        parts.append(f"{vf}setsar=1,fps={fps}[v{n}]")
        labels += f"[v{n}]"
        if any_audio:
            if info.get("audio", True):
                parts.append(f"[{n}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
            else:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={info['duration']:.3f},"
                             f"asetpts=PTS-STARTPTS[a{n}]")
            labels += f"[a{n}]"
    parts.append(f"{labels}concat=n={len(items)}:v=1:a={1 if any_audio else 0}[outv]" + ("[outa]" if any_audio else ""))
    cmd += ["-filter_complex", ";".join(parts), "-map", "[outv]"]
    if any_audio:
        cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-map_metadata", "0", "-map_chapters", "-1"]
    cmd += enc_args(enc, q, preset, any(i["tenbit"] for _, i in items))
    cmd += ["-tag:v", "hvc1", dst]
    return cmd


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("H.265 인코딩 도구")
        self.geometry("1320x680")
        self.items = {}            # iid -> dict(path, info, checked, status)
        self.q = queue.Queue()     # 워커 -> UI 메시지
        self.proc = None
        self.stop_flag = False
        self.busy = False          # 스캔 중
        self.encoding = False      # 인코딩 중 (이때도 파일 추가/대기 항목 삭제 가능)
        self.queue = []            # 대기열: 작업 목록 [[iid, ...], ...] (합치기는 iid 여러 개). 접근은 qlock 으로
        self.qlock = threading.Lock()
        self.worker_alive = False
        self.cur_job = []          # 지금 인코딩 중인 iid 들
        self.sort_rev = {}
        self._sum_pending = False
        self._ui()
        self._load_settings()
        self.after(100, self._pump)

    # ---------- UI ----------
    def _ui(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="폴더 스캔 (하위 포함)", command=self.add_folder).pack(side="left")
        ttk.Button(top, text="파일 추가", command=self.add_files).pack(side="left", padx=4)
        ttk.Button(top, text="목록 비우기", command=self.clear).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="전체 선택", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(top, text="전체 해제", command=lambda: self.set_all(False)).pack(side="left", padx=4)
        ttk.Button(top, text="H.265 아닌 것만 선택", command=self.select_non_hevc).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="원본↔결과 비교", command=self.compare).pack(side="left")
        ttk.Button(top, text="줄어든 원본 삭제", command=self.delete_originals).pack(side="left", padx=4)
        ttk.Button(top, text="커진 결과 삭제", command=self.delete_bigger).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="정렬:").pack(side="left")
        self.sort_var = tk.StringVar(value=list(SORTS)[0])
        cb = ttk.Combobox(top, textvariable=self.sort_var, values=list(SORTS), width=16, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_sort())

        opt = ttk.LabelFrame(self, text="인코딩 설정", padding=6)
        opt.pack(fill="x", padx=6)
        self.enc = tk.StringVar(value="cpu")
        for k, (name, _) in ENCODERS.items():
            ttk.Radiobutton(opt, text=name, value=k, variable=self.enc,
                            command=self.on_enc).pack(side="left", padx=8)
        ttk.Separator(opt, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(opt, text="품질값 (낮을수록 고화질·큰 용량):").pack(side="left")
        self.qual = tk.IntVar(value=23)
        ttk.Combobox(opt, textvariable=self.qual, values=list(range(16, 35)), width=4,
                     state="readonly").pack(side="left", padx=6)
        ttk.Label(opt, text="속도 프리셋:").pack(side="left", padx=(12, 0))
        self.preset = tk.StringVar(value="medium")
        ttk.Combobox(opt, textvariable=self.preset, values=PRESETS, width=8,
                     state="readonly").pack(side="left", padx=6)

        self.auto_del = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="원본보다 커지면 결과 자동 삭제", variable=self.auto_del).pack(
            side="left", padx=(12, 0))
        ttk.Label(opt, text="완료 후:").pack(side="left", padx=(12, 0))
        self.after_done = tk.StringVar(value=AFTER[0])  # 안전상 설정 저장 안 함
        ttk.Combobox(opt, textvariable=self.after_done, values=AFTER, width=10,
                     state="readonly").pack(side="left", padx=6)

        cols = ("sel", "merge", "name", "codec", "bitrate", "res", "size", "outsize", "ratio", "status")
        heads = ("선택", "합치기", "파일 (폴더)", "코덱", "비트레이트", "해상도", "원본 크기", "결과 크기", "감소율", "상태")
        widths = (50, 60, 380, 60, 90, 80, 80, 80, 70, 170)
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, anchor="w" if c in ("name", "status") else "center",
                             stretch=(c == "name"))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="left", fill="y", pady=6)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", lambda e: "break")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self.on_rclick)

        bottom = ttk.Frame(self, padding=6)
        bottom.pack(side="bottom", fill="x", before=self.tree)
        self.sumlbl = ttk.Label(bottom, text="", font=("", 10, "bold"))
        self.sumlbl.pack(fill="x", pady=(0, 4))
        for v in (self.enc, self.preset):
            v.trace_add("write", lambda *a: self.sched_summary())
        self.pb = ttk.Progressbar(bottom, maximum=100)
        self.pb.pack(fill="x")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(4, 0))
        self.msg = ttk.Label(row, text="폴더를 스캔하거나 파일을 추가하세요.")
        self.msg.pack(side="left")
        self.btn_stop = ttk.Button(row, text="중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_rm_pending = ttk.Button(row, text="대기 항목 모두 제거", command=self.remove_pending,
                                         state="disabled")
        self.btn_rm_pending.pack(side="right", padx=6)
        self.btn_go = ttk.Button(row, text="선택 항목 인코딩 시작", command=self.start)
        self.btn_go.pack(side="right", padx=6)

    def on_enc(self):
        self.qual.set(ENCODERS[self.enc.get()][1])

    # ---------- 예상 시간 ----------
    def _speed_key(self):
        return f"{self.enc.get()}|{self.preset.get()}"

    def sched_summary(self):
        if not self._sum_pending:
            self._sum_pending = True
            self.after(200, self.update_summary)

    def update_summary(self):
        """선택된(아직 안 끝난) 항목의 총 재생시간 / 예상 인코딩 시간 / 예상 완료 시각."""
        self._sum_pending = False
        sel = [it for it in self.items.values() if it["checked"]]
        if not sel:
            self.sumlbl.config(text="선택된 항목 없음")
            return
        dur = sum(it["info"]["duration"] for it in sel)
        # 작업량 = 재생시간 x 픽셀수 (해상도가 클수록 오래 걸리므로). 진행 중인 파일은 남은 비율만
        work = sum(it["info"]["duration"] * pixels(it["info"]["res"]) * (1 - it.get("pct", 0) / 100)
                   for it in sel)
        text = f"선택 {len(sel)}개  |  전체 재생시간 {fmt_dur(dur)}"
        sp = self.speed.get(self._speed_key())
        if sp:
            secs = work / sp
            end = datetime.datetime.now() + datetime.timedelta(seconds=secs)
            wd = "월화수목금토일"[end.weekday()]
            ampm = "오전" if end.hour < 12 else "오후"
            text += (f"  |  예상 인코딩 시간 {fmt_dur(secs)}"
                     f"  |  예상 완료 {end.month}/{end.day}({wd}) {ampm} {end.hour % 12 or 12}:{end.minute:02d}")
        else:
            text += "  |  예상 시간: 이 인코더/프리셋 첫 인코딩 후 계산됩니다 (속도를 측정해 저장)"
        self.sumlbl.config(text=text)

    # ---------- 설정 ----------
    def _load_settings(self):
        self.speed = {}
        self.bigger = {}
        self._old_linked = 0
        self.merges = {}  # 합친 결과 파일 경로 -> {members:[{path,size,mtime}], size, orig_size, orig_dur}
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                s = json.load(f)
            self.bigger = s.get("bigger", {})
            self.merges = s.get("merges", {})
            self.auto_del.set(bool(s.get("auto_del", True)))
            self.speed = {k: float(v) for k, v in s.get("speed", {}).items()}
            if s.get("enc") in ENCODERS:
                self.enc.set(s["enc"])
            self.qual.set(int(s.get("q", ENCODERS[self.enc.get()][1])))
            if s.get("preset") in PRESETS:
                self.preset.set(s["preset"])
        except Exception:
            self.on_enc()

    def _save_settings(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"enc": self.enc.get(), "q": self.qual.get(), "preset": self.preset.get(),
                           "speed": self.speed, "bigger": self.bigger, "merges": self.merges,
                           "auto_del": self.auto_del.get()}, f)
        except Exception:
            pass

    # ---------- 목록 ----------
    def add_folder(self):
        d = filedialog.askdirectory(title="스캔할 폴더")
        if d:
            self._scan([d], True)

    def add_files(self):
        fs = filedialog.askopenfilenames(
            title="영상 파일", filetypes=[("영상", " ".join("*" + e for e in VIDEO_EXT)), ("전체", "*.*")])
        if fs:
            self._scan(list(fs), False)

    def _scan(self, paths, folders):
        if self.busy:
            return
        self.busy = True
        self._old_linked = 0
        self.msg.config(text="스캔 중...")

        def work():
            files = []
            for p in paths:
                if folders:
                    for root, _, names in os.walk(p):
                        for n in names:
                            if os.path.splitext(n)[1].lower() in VIDEO_EXT:
                                files.append(os.path.join(root, n))
                else:
                    files.append(p)
            merged_found = [os.path.normpath(f) for f in sorted(files)
                            if os.path.basename(f).startswith(PREFIX + "합침_")]
            known = {it["path"] for it in self.items.values()}
            # 결과 파일(h265_ 접두사)은 원본 행에 연결되므로 목록에서 뺀다
            files = [f for f in sorted(files) if os.path.normpath(f) not in known
                     and not os.path.basename(f).startswith(PREFIX)]
            cache = load_cache()
            hits = 0
            for i, f in enumerate(files, 1):
                self.q.put(("scanmsg", f"스캔 중... {i}/{len(files)} (캐시 {hits})"))
                info, hit = cached_probe(f, cache)
                hits += hit
                if info:
                    self.q.put(("add", os.path.normpath(f), info, paths[0] if folders else None))
            for mp in merged_found:  # 이전에 합쳐서 만든 결과: 원본과 짝지어 감소율/원본 삭제에 쓸 수 있게
                info, hit = cached_probe(mp, cache)
                if info:
                    self.q.put(("oldmerge", mp, info))
            save_cache(cache)
            self.q.put(("scandone", len(files)))

        threading.Thread(target=work, daemon=True).start()

    def _row_values(self, it):
        i = it["info"]
        ov = out_view(it)  # (결과 크기, 비교 기준 원본 크기)
        return ("☑" if it["checked"] else "☐", str(it["merge"]) if it.get("merge") else "☐", it["label"], i["codec"], fmt_rate(i["bitrate"]),
                i["res"], fmt_size(i["size"]), fmt_size(ov[0]) if ov else "",
                f"-{100 - ov[0] / ov[1] * 100:.0f}%" if ov and ov[0] < ov[1]
                else (f"+{ov[0] / ov[1] * 100 - 100:.0f}%" if ov else ""), it["status"])

    def refresh(self, iid):
        self.tree.item(iid, values=self._row_values(self.items[iid]))
        self.sched_summary()

    def on_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        col = self.tree.identify_column(e.x)
        if col in ("#1", "#2"):
            iid = self.tree.identify_row(e.y)
            if iid and self.encoding:
                # 인코딩 중에는 체크로 대기열에 넣고 빼는 것만 가능 (진행 중인 항목/합치기 표시는 변경 불가)
                it = self.items[iid]
                if col == "#1" and iid not in self.cur_job:
                    it["checked"] = not it["checked"]
                    if it["checked"]:
                        self._enqueue([iid])
                    else:
                        self._dequeue(iid)
                    self.refresh(iid)
            elif iid and not self.busy:
                it = self.items[iid]
                if col == "#1":
                    it["checked"] ^= True
                else:  # 합치기 번호 순환 (☐ → 1 → 2 → … → ☐). 번호가 같은 행끼리 하나로 합쳐짐
                    top = max((x.get("merge") or 0 for k, x in self.items.items() if k != iid), default=0)
                    nxt = (it.get("merge") or 0) + 1
                    it["merge"] = nxt if nxt <= top + 1 else 0
                    if it["merge"]:
                        it["checked"] = True
                self.refresh(iid)
            return "break"

    def on_rclick(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="선택한 행 체크", command=lambda: self.check_selected(True))
        m.add_command(label="선택한 행 체크 해제", command=lambda: self.check_selected(False))
        m.add_separator()
        m.add_command(label="선택한 행을 하나로 합치기 (새 묶음)", command=lambda: self.mark_merge(True))
        m.add_command(label="선택한 행 합치기 해제", command=lambda: self.mark_merge(False))
        m.add_command(label="위로 이동 (합치는 순서)", command=lambda: self.move_selected(-1))
        m.add_command(label="아래로 이동 (합치는 순서)", command=lambda: self.move_selected(1))
        m.add_separator()
        m.add_command(label="목록에서 제거", command=self.remove_selected)
        m.add_command(label="폴더 열기", command=self.open_folder)
        m.tk_popup(e.x_root, e.y_root)

    def check_selected(self, v):
        for iid in self.tree.selection():
            self.items[iid]["checked"] = v
            self.refresh(iid)

    def mark_merge(self, v):
        if self.busy or self.encoding:
            return
        sel = set(self.tree.selection())
        new = max((x.get("merge") or 0 for k, x in self.items.items() if k not in sel), default=0) + 1
        for iid in sel:
            self.items[iid]["merge"] = new if v else 0
            if v:
                self.items[iid]["checked"] = True
            self.refresh(iid)

    def move_selected(self, delta):
        """선택한 행을 위/아래로 한 칸 이동 (합치기는 목록에 보이는 순서대로 이어 붙인다)."""
        if self.busy or self.encoding:
            return
        order = self.tree.get_children()
        sel = [i for i in order if i in self.tree.selection()]
        if not sel:
            return
        if delta < 0:
            if order[0] == sel[0]:
                return
        else:
            if order[-1] == sel[-1]:
                return
            sel.reverse()
        for iid in sel:
            self.tree.move(iid, "", self.tree.index(iid) + delta)

    def _enqueue(self, iids):
        """인코딩 중 대기열 맨 뒤에 추가 (작업자가 이미 끝나는 중이면 추가하지 않음 - 다시 시작을 눌러야 함)."""
        with self.qlock:
            if not self.worker_alive:
                return
            self.queue.append(list(iids))
        for i in iids:
            self.items[i]["status"] = "대기 중"
            self.refresh(i)

    def _dequeue(self, iid):
        """대기열에서 항목을 뺀다. 이미 인코딩이 시작된 항목은 영향 없음."""
        with self.qlock:
            for job in self.queue:
                if iid in job:
                    job.remove(iid)
            self.queue[:] = [j for j in self.queue if j]
        it = self.items.get(iid)
        if it and it["status"] == "대기 중":
            it["status"] = ""

    def remove_pending(self):
        """인코딩 중 아직 시작하지 않은(대기 중인) 항목을 전부 목록에서 제거."""
        with self.qlock:
            ids = [i for job in self.queue for i in job]
        if not ids:
            self.msg.config(text="대기 중인 항목이 없습니다")
            return
        if not messagebox.askyesno("대기 항목 제거",
                                   f"아직 인코딩하지 않은 대기 항목 {len(ids)}개를 목록에서 제거합니다.\n"
                                   f"(파일은 지우지 않고 지금 인코딩 중인 것은 계속합니다)\n\n진행할까요?"):
            return
        for iid in ids:
            self._dequeue(iid)
            if iid in self.items and iid not in self.cur_job:
                self.items.pop(iid)
                self.tree.delete(iid)
        self.sched_summary()
        self.msg.config(text=f"대기 항목 {len(ids)}개 제거")

    def remove_selected(self):
        if self.busy:
            return
        skipped = 0
        for iid in self.tree.selection():
            if self.encoding and iid in self.cur_job:
                skipped += 1  # 지금 인코딩 중인 파일은 못 지움
                continue
            self._dequeue(iid)
            self.items.pop(iid, None)
            self.tree.delete(iid)
        if skipped:
            self.msg.config(text="인코딩 중인 항목은 제거할 수 없습니다 (중지 후 제거)")
        self.sched_summary()

    def open_folder(self):
        sel = self.tree.selection()
        if sel:
            subprocess.Popen(["explorer", "/select,", self.items[sel[0]]["path"]])

    def clear(self):
        if self.busy or self.encoding:
            return
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self.pb["value"] = 0
        self.sched_summary()

    def set_all(self, v):  # 전체 선택에서도 "예전에 커졌던" 파일은 제외 (직접 체크해야 함)
        if self.encoding:
            return
        for iid, it in self.items.items():
            it["checked"] = v and not it.get("bigger")
            self.refresh(iid)

    def select_non_hevc(self):
        if self.encoding:
            return
        for iid, it in self.items.items():
            it["checked"] = it["info"]["codec"] != "hevc" and it["status"] == "" and not it.get("bigger")
            self.refresh(iid)

    def apply_sort(self):
        keyf, rev = SORTS[self.sort_var.get()]
        for n, iid in enumerate(sorted(self.items, key=lambda i: keyf(self.items[i]), reverse=rev)):
            self.tree.move(iid, "", n)

    def sort_by(self, col):
        def key(iid):
            it = self.items[iid]
            i = it["info"]
            ov = out_view(it)
            return {"sel": it["checked"], "merge": it.get("merge") or 0, "name": it["label"].lower(),
                    "codec": i["codec"], "bitrate": i["bitrate"], "res": i["res"], "size": i["size"],
                    "outsize": ov[0] if ov else 0, "ratio": ov[0] / ov[1] if ov else 9,
                    "status": it["status"]}[col]

        rev = self.sort_rev[col] = not self.sort_rev.get(col, True)
        for n, iid in enumerate(sorted(self.items, key=key, reverse=rev)):
            self.tree.move(iid, "", n)

    # ---------- 인코딩 ----------
    def out_path(self, src):
        d, name = os.path.split(src)
        base, ext = os.path.splitext(name)
        ext = ext if ext.lower() in (".mp4", ".mkv") else ".mkv"
        return os.path.join(d, PREFIX + base + ext)

    def merge_path(self, its):
        d, name = os.path.split(its[0]["path"])
        base, ext = os.path.splitext(name)
        ext = ext if ext.lower() in (".mp4", ".mkv") else ".mkv"
        return os.path.join(d, f"{PREFIX}합침_{base}_외{len(its) - 1}개{ext}")

    def start(self):
        if self.busy or self.encoding:
            return
        todo = [iid for iid in self.tree.get_children() if self.items[iid]["checked"]]
        if not todo:
            messagebox.showinfo("인코딩", "선택된 파일이 없습니다.")
            return
        warn = [iid for iid in todo if self.items[iid].get("bigger")]
        if warn:  # 예전에 인코딩했더니 원본보다 커졌던 파일이 체크돼 있으면 알려 준다
            lines = "\n".join(f"· {self.items[i]['label']}  —  {self._bigger_text(self.items[i]['bigger'])}"
                              for i in warn[:8])
            more = f"\n… 외 {len(warn) - 8}개" if len(warn) > 8 else ""
            ans = messagebox.askyesnocancel(
                "예전에 커졌던 파일",
                f"체크한 파일 중 {len(warn)}개는 예전에 인코딩했더니 원본보다 커져서 결과를 지운 적이 있습니다.\n\n"
                f"{lines}{more}\n\n예 = 그래도 전부 인코딩\n아니오 = 이 파일들만 빼고 진행\n취소 = 시작 안 함")
            if ans is None:
                return
            if ans is False:
                for i in warn:
                    self.items[i]["checked"] = False
                    self.refresh(i)
                todo = [i for i in todo if i not in warn]
                if not todo:
                    return
        # 작업 목록: 같은 합치기 번호의 항목(2개 이상)은 목록 순서대로 하나의 작업, 나머지는 각각 하나씩
        groups = {}
        for i in todo:
            g = self.items[i].get("merge") or 0
            if g:
                groups.setdefault(g, []).append(i)
        groups = {g: ids for g, ids in groups.items() if len(ids) >= 2}
        if groups:
            blocks = []
            for g, ids in sorted(groups.items()):
                its = [self.items[i] for i in ids]
                lines = "\n".join(f"   {n}. {it['label']}" for n, it in enumerate(its[:8], 1))
                more = f"\n   … 외 {len(its) - 8}개" if len(its) > 8 else ""
                res = sorted({it["info"]["res"] for it in its})
                warn = (f"\n   ※ 해상도가 다름 ({', '.join(res)}) → 첫 영상 {its[0]['info']['res']} 기준, 남는 곳은 검은 여백"
                        if len(res) > 1 else "")
                blocks.append(f"[묶음 {g}] {len(its)}개 → {os.path.basename(self.merge_path(its))}\n{lines}{more}{warn}")
            n_single = len(todo) - sum(len(v) for v in groups.values())
            if not messagebox.askokcancel(
                    "합치기",
                    f"합치기 묶음 {len(groups)}개를 각각 파일 1개로 이어 붙여 인코딩합니다.\n\n" + "\n\n".join(blocks)
                    + f"\n\n나머지 {n_single}개는 각각 따로 인코딩합니다.\n"
                    f"(합치면 자막/챕터는 버려지고 음성은 AAC로 다시 인코딩됩니다)"):
                return
            jobs, placed = [], set()
            for i in todo:
                g = self.items[i].get("merge") or 0
                if g in groups:
                    if g not in placed:
                        jobs.append(groups[g])
                        placed.add(g)
                else:
                    jobs.append([i])
        else:
            jobs = [[i] for i in todo]
        self._save_settings()
        self.encoding = True
        self.stop_flag = False
        with self.qlock:
            self.queue = [list(j) for j in jobs]
            self.worker_alive = True
        for job in jobs:
            for i in job:
                self.items[i]["status"] = "대기 중"
                self.refresh(i)
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_rm_pending.config(state="normal")
        enc, qv, pre = self.enc.get(), self.qual.get(), self.preset.get()
        threading.Thread(target=self._worker, args=(enc, qv, pre, self.auto_del.get()), daemon=True).start()

    def stop(self):
        self.stop_flag = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _worker(self, enc, qv, pre, auto_del):
        n = 0
        ok = 0
        while True:
            with self.qlock:
                if self.stop_flag or not self.queue:
                    self.worker_alive = False
                    break
                iids = self.queue.pop(0)
                total = n + 1 + len(self.queue)
            n += 1
            self.cur_job = iids
            its = [self.items[i] for i in iids]
            merge = len(iids) > 1
            infos = [it["info"] for it in its]
            duration = sum(i["duration"] for i in infos)
            orig_size = sum(i["size"] for i in infos)

            def status(text):
                for i in iids:
                    self.q.put(("status", i, text))

            if merge:
                dst = self.merge_path(its)
                name = f"합치기 {len(its)}개 → {os.path.basename(dst)}"
            else:
                dst = self.out_path(its[0]["path"])
                name = os.path.basename(its[0]["path"])
            status("합치는 중 0%" if merge else "인코딩 중 0%")
            self.q.put(("msg", f"[{n}/{total}] {name}"))
            if os.path.exists(dst):
                status("건너뜀 (결과 파일 있음)")
                continue
            part = dst + ".part" + os.path.splitext(dst)[1]
            if merge:
                cmd = build_merge_cmd([(it["path"], it["info"]) for it in its], part, enc, qv, pre)
            else:
                cmd = build_cmd(its[0]["path"], part, enc, qv, pre, infos[0])
            tail = []
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             creationflags=NOWIN)
                for raw in self.proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("out_time_us=") and duration:
                        try:
                            pct = min(99, int(line[12:]) / 1e6 / duration * 100)
                        except ValueError:
                            continue
                        total = n + len(self.queue)  # 인코딩 중 추가/제거된 만큼 전체 개수가 바뀜
                        self.q.put(("prog", iids, pct, (n - 1 + pct / 100) / total * 100, merge))
                    elif "=" not in line or line.startswith(("Error", "[")):
                        tail.append(line)
                        tail = tail[-15:]
                rc = self.proc.wait()
            except Exception as e:
                rc, tail = -1, [str(e)]
            if self.stop_flag:
                self._rm(part)
                status("중지됨")
                break
            if rc == 0 and os.path.exists(part):
                os.replace(part, dst)
                try:
                    copy_times(its[0]["path"], dst)
                except Exception:
                    pass
                el = time.time() - t0
                if el > 3 and duration:  # 너무 짧은 파일은 오차가 커서 제외
                    self.q.put(("speed", f"{enc}|{pre}",
                                sum(i["duration"] * pixels(i["res"]) for i in infos) / el))
                new = os.path.getsize(dst)
                if new >= orig_size:  # 원본보다 커지면 인코딩 의미가 없음
                    pct = new / orig_size * 100 - 100
                    if auto_del and delete_file(dst):
                        if not merge:
                            self.q.put(("bigger", iids[0], new, enc, qv, pre))
                        status(f"커져서 삭제됨 (+{pct:.0f}%)")
                        continue
                    if not merge:
                        self.q.put(("out", iids[0], dst))
                    status(f"커짐 (+{pct:.0f}%) 결과 삭제 권장")
                    ok += 1
                    continue
                if merge:
                    self.q.put(("mout", iids, dst, new, orig_size, duration))
                    status(f"합침 완료 (-{100 - new / orig_size * 100:.0f}%)")
                else:
                    self.q.put(("out", iids[0], dst))
                    status("완료")
                ok += 1
            else:
                self._rm(part)
                status("실패")
                self.q.put(("log", name, "\n".join(tail)))
        with self.qlock:
            self.worker_alive = False
        self.cur_job = []
        self.q.put(("done", ok, n))

    @staticmethod
    def _rm(p):
        try:
            os.remove(p)
        except OSError:
            pass

    # ---------- 비교 / 원본 삭제 ----------
    def _pairs(self, only_selected=False):
        ids = self.tree.selection() if only_selected and self.tree.selection() else self.tree.get_children()
        return [(iid, self.items[iid]) for iid in ids if self.items[iid].get("out")]

    def compare(self):
        pairs = self._pairs(True)
        if not pairs:
            messagebox.showinfo("비교", "결과 파일이 있는 항목이 없습니다.")
            return
        iid, it = pairs[0]
        o_info = probe(it["out"]["path"]) or {}
        i = it["info"]
        w = tk.Toplevel(self)
        w.title("원본 ↔ 결과 비교")
        w.geometry("760x300")
        f = ttk.Frame(w, padding=10)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=it["label"], font=("", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        rows = [("", "원본", "결과"),
                ("코덱", i["codec"], o_info.get("codec", "?")),
                ("비트레이트", fmt_rate(i["bitrate"]), fmt_rate(o_info.get("bitrate"))),
                ("해상도", i["res"], o_info.get("res", "?")),
                ("길이", f"{i['duration']:.1f}초", f"{o_info.get('duration', 0):.1f}초"),
                ("크기", fmt_size(i["size"]), fmt_size(it["out"]["size"]))]
        for r, row in enumerate(rows, 1):
            for c, t in enumerate(row):
                ttk.Label(f, text=t, font=("", 10, "bold") if r == 1 else None).grid(
                    row=r, column=c, sticky="w", padx=12, pady=2)
        diff = 100 - it["out"]["size"] / i["size"] * 100
        ttk.Label(f, text=(f"{diff:.1f}% 줄었습니다" if diff > 0 else f"{-diff:.1f}% 늘어났습니다"),
                  font=("", 12, "bold")).grid(row=8, column=0, columnspan=3, pady=10, sticky="w")
        bf = ttk.Frame(f)
        bf.grid(row=9, column=0, columnspan=3, sticky="w")
        ttk.Button(bf, text="원본 재생", command=lambda: os.startfile(it["path"])).pack(side="left")
        ttk.Button(bf, text="결과 재생", command=lambda: os.startfile(it["out"]["path"])).pack(side="left", padx=6)
        ttk.Button(bf, text="원본 폴더 열기",
                   command=lambda: subprocess.Popen(["explorer", "/select,", it["path"]])).pack(side="left")

    def _mark_bigger(self, it, pct, enc=None, q=None, pre=None):
        """결과가 원본보다 커졌던 기록을 남긴다 (원본의 크기+수정시각이 같은 동안만 유효)."""
        try:
            st = os.stat(it["path"])
        except OSError:
            return
        rec = {"size": st.st_size, "mtime": st.st_mtime, "pct": round(pct),
               "enc": enc, "q": q, "preset": pre, "date": datetime.date.today().isoformat()}
        self.bigger[it["path"]] = rec
        it["bigger"] = rec
        self._save_settings()

    def _set_merge(self, iids, out, out_size, orig_size, dur, status=None):
        """합친 결과를 멤버 항목들에 연결하고 설정에 기록한다 (새로 합쳤을 때 / 예전 결과를 찾았을 때 공용)."""
        paths = [self.items[i]["path"] for i in iids]
        rec = {"members": [], "size": out_size, "orig_size": orig_size, "orig_dur": dur}
        for pth in paths:
            try:
                st = os.stat(pth)
                rec["members"].append({"path": pth, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass
        self.merges[out] = rec
        self._save_settings()
        for iid in iids:
            it = self.items[iid]
            it["mout"] = {"path": out, "size": out_size, "orig_size": orig_size, "orig_dur": dur, "members": paths}
            it["checked"] = False
            if status:
                it["status"] = status
            self.refresh(iid)

    def _link_old_merge(self, path, info):
        """`h265_합침_<첫파일>_외N개.확장자` 를 같은 폴더의 원본과 짝짓는다. 첫 파일부터 이름순으로 N+1개의 길이
        합이 합친 결과의 길이와 맞을 때만 연결 (원본 삭제 때 다시 한 번 검증함)."""
        if path in self.merges:
            return
        mt = re.match(r"^" + re.escape(PREFIX) + r"합침_(.+)_외(\d+)개\.(mp4|mkv)$", os.path.basename(path), re.I)
        if not mt:
            return
        base, n = mt.group(1), int(mt.group(2)) + 1
        d = os.path.normcase(os.path.dirname(path))
        same = sorted(((k, x) for k, x in self.items.items() if os.path.normcase(os.path.dirname(x["path"])) == d),
                      key=lambda kx: kx[1]["label"].lower())
        idx = next((n_ for n_, (k, x) in enumerate(same)
                    if os.path.splitext(os.path.basename(x["path"]))[0] == base), None)
        if idx is None:
            return
        cand = same[idx: idx + n]
        if len(cand) != n or any(x.get("mout") for _, x in cand):
            return
        dur = sum(x["info"]["duration"] for _, x in cand)
        if abs(dur - info["duration"]) > max(2, 0.5 * n):
            return
        orig = sum(x["info"]["size"] for _, x in cand)
        pct = info["size"] / orig * 100 - 100
        self._set_merge([k for k, _ in cand], path, info["size"], orig, dur,
                        "합쳐서 인코딩됨 (" + (f"+{pct:.0f}%)" if pct >= 0 else f"-{-pct:.0f}%)"))
        self._old_linked += 1

    def _merge_record(self, path):
        """이 파일이 예전에 합쳐서 인코딩된 적이 있고(원본이 그대로이고 결과 파일도 그대로면) 그 기록을 돌려준다."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        for out, r in self.merges.items():
            mem = next((x for x in r["members"] if x["path"] == path), None)
            if not mem or mem["size"] != st.st_size or mem["mtime"] != st.st_mtime:
                continue
            try:
                if os.path.getsize(out) != r["size"]:
                    continue
            except OSError:
                continue
            return {"path": out, "size": r["size"], "orig_size": r["orig_size"], "orig_dur": r["orig_dur"],
                    "members": [x["path"] for x in r["members"]]}
        return None

    def _was_bigger(self, path):
        b = self.bigger.get(path)
        try:
            st = os.stat(path)
        except OSError:
            return None
        if isinstance(b, dict) and b.get("size") == st.st_size and b.get("mtime") == st.st_mtime:
            return b
        return None

    @staticmethod
    def _bigger_text(rec):
        opts = ", ".join(str(x) for x in (ENCODERS.get(rec.get("enc"), ("",))[0].split()[0] if rec.get("enc") else None,
                                          f"품질 {rec['q']}" if rec.get("q") is not None else None,
                                          rec.get("preset")) if x)
        return f"+{rec.get('pct', 0)}% 커짐" + (f" ({opts}, {rec.get('date', '')})" if opts else f" ({rec.get('date', '')})")

    def delete_bigger(self):
        """결과가 원본보다 크거나 같은 항목의 결과 파일을 휴지통으로 (원본은 그대로)."""
        cand = [(iid, it) for iid, it in self.items.items()
                if it.get("out") and it["out"]["size"] >= it["info"]["size"]]
        if not cand:
            messagebox.showinfo("커진 결과 삭제", "원본보다 커진 결과 파일이 없습니다.")
            return
        extra = sum(it["out"]["size"] - it["info"]["size"] for _, it in cand)
        if not messagebox.askyesno(
                "커진 결과 삭제",
                f"결과가 원본보다 커진 {len(cand)}개의 결과 파일(h265_...)을 완전 삭제합니다 (휴지통 안 거침).\n"
                f"원본은 그대로 둡니다. (결과가 원본보다 총 {fmt_size(extra)} 더 큼)\n\n진행할까요?"):
            return
        fails = 0
        for iid, it in cand:
            if delete_file(it["out"]["path"]):
                pct = it["out"]["size"] / it["info"]["size"] * 100 - 100
                it.pop("out")
                it["status"] = "커져서 삭제됨"
                self._mark_bigger(it, pct)
                self.refresh(iid)
            else:
                fails += 1
        self.msg.config(text=f"커진 결과 {len(cand) - fails}개 삭제" + (f" (실패 {fails})" if fails else ""))

    def delete_originals(self):
        """줄어든 결과가 있는 원본 중 체크된 것을 휴지통으로. 길이가 다르면 안전상 제외.
        합쳐서 인코딩한 묶음은 멤버 중 하나만 체크해도 묶음 전체 원본을 지운다 (합친 결과를 검증한 뒤)."""
        cand, skipped, merged_ok, groups = [], [], {}, {}
        for iid in self.tree.get_children():
            it = self.items[iid]
            o = it.get("out")
            if it["checked"] and not o and it.get("mout"):  # 합쳐서 인코딩한 멤버: 합친 파일을 검증
                m = it["mout"]
                if m["path"] not in merged_ok:
                    mi = probe(m["path"]) if os.path.exists(m["path"]) else None
                    if m["size"] >= m["orig_size"]:
                        merged_ok[m["path"]] = "합친 결과가 안 줄어듦"
                    elif not mi or abs(mi["duration"] - m["orig_dur"]) > max(2, 0.5 * len(m["members"])):
                        merged_ok[m["path"]] = "합친 결과 길이 불일치/손상"
                    else:
                        merged_ok[m["path"]] = ""
                        # 묶음 전체: 목록에 남아 있는 멤버를 전부 대상으로 (체크 여부와 상관없이)
                        for k, x in self.items.items():
                            if x.get("mout") and x["mout"]["path"] == m["path"] and k not in [c[0] for c in cand]:
                                cand.append((k, x))
                        groups[m["path"]] = m
                if merged_ok[m["path"]] and it["label"] not in " ".join(skipped):
                    skipped.append(f"{it['label']}  ({merged_ok[m['path']]})")
                continue
            if not it["checked"] or not o:
                continue
            if o["size"] >= it["info"]["size"]:
                skipped.append(f"{it['label']}  (안 줄어듦)")
                continue
            oi = probe(o["path"])
            if not oi or abs(oi["duration"] - it["info"]["duration"]) > 2:
                skipped.append(f"{it['label']}  (결과 길이 불일치/손상)")
                continue
            cand.append((iid, it))
        if not cand:
            messagebox.showinfo("원본 삭제", "삭제 대상이 없습니다.\n(체크된 항목 중 결과가 있고 크기가 줄어든 것만 대상)"
                                + ("\n\n제외:\n" + "\n".join(skipped[:10]) if skipped else ""))
            return
        saved = sum(it["info"]["size"] - it["out"]["size"] for _, it in cand if it.get("out"))
        lines = []
        for path, m in groups.items():
            mem = [it for _, it in cand if it.get("mout") and it["mout"]["path"] == path]
            orig = sum(it["info"]["size"] for it in mem)
            saved += max(0, orig - m["size"])  # 합친 파일 크기를 뺀 순수 확보 용량
            missing = len(m["members"]) - len(mem)
            lines.append(f"· 합친 묶음 {len(mem)}개 → {os.path.basename(path)}  (-{100 - m['size'] / m['orig_size'] * 100:.0f}%)"
                         + (f"  ※ 목록에 없는 원본 {missing}개는 삭제 안 됨" if missing > 0 else ""))
        msg = (f"원본 {len(cand)}개를 휴지통으로 보냅니다.\n확보 용량: {fmt_size(saved)}"
               + ("\n\n" + "\n".join(lines) if lines else "")
               + (f"\n\n제외 {len(skipped)}개:\n" + "\n".join(skipped[:10]) if skipped else ""))
        if not messagebox.askyesno("원본 삭제", msg + "\n\n진행할까요?"):
            return
        fails = 0
        for iid, it in cand:
            if trash(it["path"]):
                self.tree.delete(iid)
                del self.items[iid]
            else:
                fails += 1
        self.msg.config(text=f"원본 {len(cand) - fails}개 삭제, {fmt_size(saved)} 확보"
                             + (f" (실패 {fails})" if fails else ""))

    def confirm_shutdown(self, secs=60):
        """종료 전 카운트다운. 취소 누르면 종료하지 않고 옵션도 '아무것도 안 함'으로 되돌림."""
        w = tk.Toplevel(self)
        w.title("시스템 종료")
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        lbl = ttk.Label(w, padding=16)
        lbl.pack()

        def cancel():
            state["on"] = False
            self.after_done.set(AFTER[0])
            w.destroy()

        ttk.Button(w, text="취소", command=cancel).pack(pady=(0, 12))
        state = {"on": True}

        def tick(n):
            if not state["on"]:
                return
            if n <= 0:
                subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=NOWIN)
                return
            lbl.config(text=f"인코딩 완료. {n}초 후 시스템을 종료합니다.")
            w.after(1000, tick, n - 1)

        tick(secs)

    # ---------- 워커 메시지 처리 ----------
    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "add":
                    _, path, info, root = m
                    label = os.path.relpath(path, root) if root else os.path.basename(path)
                    it = {"path": path, "info": info, "checked": info["codec"] != "hevc",
                          "status": "", "label": label, "merge": 0}
                    dst = self.out_path(path)
                    if os.path.exists(dst):  # 이전에 만든 결과가 있으면 비교 대상으로 연결
                        it["out"] = {"path": dst, "size": os.path.getsize(dst)}
                        it["checked"] = False
                        it["status"] = "결과 있음"
                    elif self._merge_record(path):  # 예전에 합쳐서 인코딩한 파일 -> 결과/감소율 복원
                        mo = self._merge_record(path)
                        it["mout"] = mo
                        it["checked"] = False
                        pct = mo["size"] / mo["orig_size"] * 100 - 100
                        it["status"] = "합쳐서 인코딩됨 (" + (f"{pct:+.0f}%)" if pct >= 0 else f"-{-pct:.0f}%)")
                    else:
                        rec = self._was_bigger(path)
                        if rec:  # 예전에 인코딩했더니 커졌던 파일 → 기본 제외, 수동으로 체크하면 경고
                            it["bigger"] = rec
                            it["checked"] = False
                            it["status"] = f"이전에 {rec['pct']}% 커져서 제외"
                    iid = self.tree.insert("", "end")
                    self.items[iid] = it
                    self.refresh(iid)
                    if self.encoding and it["checked"]:  # 인코딩 중 추가한 파일은 대기열 맨 뒤로
                        self._enqueue([iid])
                elif k == "bigger":  # 결과가 원본보다 커서 자동 삭제됨 → 다음 스캔 때도 건너뛰도록 기록
                    it = self.items[m[1]]
                    it["checked"] = False
                    self._mark_bigger(it, m[2] / it["info"]["size"] * 100 - 100, m[3], m[4], m[5])
                    self.refresh(m[1])
                elif k == "out":
                    self.items[m[1]]["out"] = {"path": m[2], "size": os.path.getsize(m[2])}
                    self.items[m[1]]["checked"] = False
                    it = self.items[m[1]]
                    if it["out"]["size"] < it["info"]["size"]:  # 이번엔 줄었으면 예전 '커짐' 기록 해제
                        it.pop("bigger", None)
                        self.bigger.pop(it["path"], None)
                    self.refresh(m[1])
                elif k == "scanmsg" or k == "msg":
                    if k == "msg" or not self.encoding:
                        self.msg.config(text=m[1])
                elif k == "scandone":
                    self.busy = False
                    if self.encoding:  # 인코딩 중에는 정렬하지 않아 새 파일이 맨 뒤에 남음
                        self.sched_summary()
                    else:
                        self.apply_sort()
                        self.msg.config(text=f"목록 {len(self.items)}개 (스캔 {m[1]}개)"
                                        + (f", 이전 합치기 결과 {self._old_linked}개 연결" if self._old_linked else ""))
                elif k == "status":
                    self.items[m[1]]["status"] = m[2]
                    if not m[2].startswith(("인코딩 중", "합치는 중")):
                        self.items[m[1]]["pct"] = 0
                    self.refresh(m[1])
                elif k == "prog":
                    for iid in m[1]:
                        self.items[iid]["status"] = f"{'합치는' if m[4] else '인코딩'} 중 {m[2]:.0f}%"
                        self.items[iid]["pct"] = m[2]
                        self.refresh(iid)
                    self.pb["value"] = m[3]
                elif k == "mout":  # 합치기 결과: 멤버마다 기록해 두고 '줄어든 원본 삭제'에서 검증해 사용
                    self._set_merge(m[1], m[2], m[3], m[4], m[5])
                elif k == "oldmerge":
                    self._link_old_merge(m[1], m[2])
                elif k == "speed":  # 파일 하나 끝날 때마다 실측 속도를 이동평균으로 저장
                    old = self.speed.get(m[1])
                    self.speed[m[1]] = m[2] if not old else old * 0.5 + m[2] * 0.5
                    self._save_settings()
                    self.sched_summary()
                elif k == "log":
                    messagebox.showerror("인코딩 실패", f"{m[1]}\n\n{m[2][-1200:]}")
                elif k == "done":
                    self.encoding = False
                    self.proc = None
                    with self.qlock:
                        self.queue.clear()
                    for iid, it in self.items.items():  # 중지로 남은 대기 표시 정리
                        if it["status"] == "대기 중":
                            it["status"] = ""
                            self.refresh(iid)
                    self.btn_go.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_rm_pending.config(state="disabled")
                    self.pb["value"] = 100 if m[1] == m[2] else self.pb["value"]
                    self.msg.config(text=f"끝: {m[1]}/{m[2]}개 완료")
                    if self.after_done.get() == AFTER[1] and not self.stop_flag:
                        self.confirm_shutdown()
        except queue.Empty:
            pass
        self.after(100, self._pump)


if __name__ == "__main__":
    App().mainloop()

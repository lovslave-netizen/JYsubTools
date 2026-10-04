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
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    size = int(fmt.get("size") or os.path.getsize(path))
    br = int(v.get("bit_rate") or 0)
    if not br and dur:  # 컨테이너에 영상 비트레이트가 없으면 전체 비트레이트로 대체
        br = int(fmt.get("bit_rate") or size * 8 / dur)
    return {
        "codec": v.get("codec_name", "?"),
        "bitrate": br,
        "res": f"{v.get('width', '?')}x{v.get('height', '?')}",
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
    "감소율 큰순": (lambda it: (1 - it["out"]["size"] / it["info"]["size"]) if it.get("out") else -9, True),
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
    if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
        return c["info"], 1
    info = probe(path)
    if info:
        cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "info": info}
    return info, 0


def build_cmd(src, dst, enc, q, preset, info):
    ext = os.path.splitext(dst)[1].lower()
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-y", "-progress", "pipe:1", "-i", src]
    if ext == ".mkv":
        cmd += ["-map", "0", "-c", "copy"]
    else:  # mp4 등: 영상/오디오/자막(mov_text)만
        cmd += ["-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-c:a", "copy", "-c:s", "mov_text"]
    cmd += ["-map_metadata", "0"]
    ten = info["tenbit"]
    if enc == "cpu":
        cmd += ["-c:v", "libx265", "-crf", str(q), "-preset", preset,
                "-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
    elif enc == "qsv":
        cmd += ["-c:v", "hevc_qsv", "-global_quality", str(q), "-preset", preset,
                "-pix_fmt", "p010le" if ten else "nv12"]
    else:
        cmd += ["-c:v", "hevc_amf", "-quality", AMF_QUALITY[preset], "-rc", "cqp",
                "-qp_i", str(q), "-qp_p", str(q + 2),
                "-pix_fmt", "p010le" if ten else "nv12"]
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
        self.busy = False
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

        cols = ("sel", "name", "codec", "bitrate", "res", "size", "outsize", "ratio", "status")
        heads = ("선택", "파일 (폴더)", "코덱", "비트레이트", "해상도", "원본 크기", "결과 크기", "감소율", "상태")
        widths = (50, 380, 60, 90, 80, 80, 80, 70, 150)
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
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                s = json.load(f)
            self.bigger = s.get("bigger", {})
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
                           "speed": self.speed, "bigger": self.bigger,
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
            save_cache(cache)
            self.q.put(("scandone", len(files)))

        threading.Thread(target=work, daemon=True).start()

    def _row_values(self, it):
        i = it["info"]
        o = it.get("out")
        return ("☑" if it["checked"] else "☐", it["label"], i["codec"], fmt_rate(i["bitrate"]),
                i["res"], fmt_size(i["size"]), fmt_size(o["size"]) if o else "",
                f"-{100 - o['size'] / i['size'] * 100:.0f}%" if o and o["size"] < i["size"]
                else (f"+{o['size'] / i['size'] * 100 - 100:.0f}%" if o else ""), it["status"])

    def refresh(self, iid):
        self.tree.item(iid, values=self._row_values(self.items[iid]))
        self.sched_summary()

    def on_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) == "#1":
            iid = self.tree.identify_row(e.y)
            if iid and not self.busy:
                self.items[iid]["checked"] ^= True
                self.refresh(iid)
            return "break"

    def on_rclick(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="선택한 행 체크", command=lambda: self.check_selected(True))
        m.add_command(label="선택한 행 체크 해제", command=lambda: self.check_selected(False))
        m.add_command(label="목록에서 제거", command=self.remove_selected)
        m.add_command(label="폴더 열기", command=self.open_folder)
        m.tk_popup(e.x_root, e.y_root)

    def check_selected(self, v):
        for iid in self.tree.selection():
            self.items[iid]["checked"] = v
            self.refresh(iid)

    def remove_selected(self):
        if self.busy:
            return
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.tree.delete(iid)
        self.sched_summary()

    def open_folder(self):
        sel = self.tree.selection()
        if sel:
            subprocess.Popen(["explorer", "/select,", self.items[sel[0]]["path"]])

    def clear(self):
        if self.busy:
            return
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self.pb["value"] = 0
        self.sched_summary()

    def set_all(self, v):  # 전체 선택에서도 "예전에 커졌던" 파일은 제외 (직접 체크해야 함)
        for iid, it in self.items.items():
            it["checked"] = v and not it.get("bigger")
            self.refresh(iid)

    def select_non_hevc(self):
        for iid, it in self.items.items():
            it["checked"] = it["info"]["codec"] != "hevc" and it["status"] == "" and not it.get("bigger")
            self.refresh(iid)

    def apply_sort(self):
        keyf, rev = SORTS[self.sort_var.get()]
        for n, iid in enumerate(sorted(self.items, key=lambda i: keyf(self.items[i]), reverse=rev)):
            self.tree.move(iid, "", n)

    def sort_by(self, col):
        idx = {"sel": 0, "name": 1, "codec": 2, "bitrate": 3, "res": 4, "size": 5, "outsize": 6,
               "ratio": 7, "status": 8}[col]

        def key(iid):
            it = self.items[iid]
            i = it["info"]
            o = it.get("out")
            return {0: it["checked"], 1: it["label"].lower(), 2: i["codec"], 3: i["bitrate"],
                    4: i["res"], 5: i["size"], 6: o["size"] if o else 0,
                    7: o["size"] / i["size"] if o else 9, 8: it["status"]}[idx]

        rev = self.sort_rev[col] = not self.sort_rev.get(col, True)
        for n, iid in enumerate(sorted(self.items, key=key, reverse=rev)):
            self.tree.move(iid, "", n)

    # ---------- 인코딩 ----------
    def out_path(self, src):
        d, name = os.path.split(src)
        base, ext = os.path.splitext(name)
        ext = ext if ext.lower() in (".mp4", ".mkv") else ".mkv"
        return os.path.join(d, PREFIX + base + ext)

    def start(self):
        if self.busy:
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
        self._save_settings()
        self.busy = True
        self.stop_flag = False
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")
        enc, qv, pre = self.enc.get(), self.qual.get(), self.preset.get()
        threading.Thread(target=self._worker, args=(todo, enc, qv, pre, self.auto_del.get()), daemon=True).start()

    def stop(self):
        self.stop_flag = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _worker(self, todo, enc, qv, pre, auto_del):
        total = len(todo)
        ok = 0
        for n, iid in enumerate(todo, 1):
            if self.stop_flag:
                break
            it = self.items[iid]
            src, info = it["path"], it["info"]
            dst = self.out_path(src)
            self.q.put(("status", iid, "인코딩 중 0%"))
            self.q.put(("msg", f"[{n}/{total}] {os.path.basename(src)}"))
            if os.path.exists(dst):
                self.q.put(("status", iid, "건너뜀 (결과 파일 있음)"))
                continue
            part = dst + ".part" + os.path.splitext(dst)[1]
            cmd = build_cmd(src, part, enc, qv, pre, info)
            tail = []
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             creationflags=NOWIN)
                for raw in self.proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("out_time_us=") and info["duration"]:
                        try:
                            pct = min(99, int(line[12:]) / 1e6 / info["duration"] * 100)
                        except ValueError:
                            continue
                        self.q.put(("prog", iid, pct, (n - 1 + pct / 100) / total * 100))
                    elif "=" not in line or line.startswith(("Error", "[")):
                        tail.append(line)
                        tail = tail[-15:]
                rc = self.proc.wait()
            except Exception as e:
                rc, tail = -1, [str(e)]
            if self.stop_flag:
                self._rm(part)
                self.q.put(("status", iid, "중지됨"))
                break
            if rc == 0 and os.path.exists(part):
                os.replace(part, dst)
                try:
                    copy_times(src, dst)
                except Exception:
                    pass
                el = time.time() - t0
                if el > 3 and info["duration"]:  # 너무 짧은 파일은 오차가 커서 제외
                    self.q.put(("speed", f"{enc}|{pre}", info["duration"] * pixels(info["res"]) / el))
                new = os.path.getsize(dst)
                if new >= info["size"]:  # 원본보다 커지면 인코딩 의미가 없음
                    pct = new / info["size"] * 100 - 100
                    if auto_del and delete_file(dst):
                        self.q.put(("bigger", iid, new, enc, qv, pre))
                        self.q.put(("status", iid, f"커져서 삭제됨 (+{pct:.0f}%)"))
                        continue
                    self.q.put(("out", iid, dst))
                    self.q.put(("status", iid, f"커짐 (+{pct:.0f}%) 결과 삭제 권장"))
                    ok += 1
                    continue
                self.q.put(("out", iid, dst))
                self.q.put(("status", iid, "완료"))
                ok += 1
            else:
                self._rm(part)
                self.q.put(("status", iid, "실패"))
                self.q.put(("log", os.path.basename(src), "\n".join(tail)))
        self.q.put(("done", ok, total))

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
        """줄어든 결과가 있는 원본 중 체크된 것을 휴지통으로. 길이가 다르면 안전상 제외."""
        cand, skipped = [], []
        for iid in self.tree.get_children():
            it = self.items[iid]
            o = it.get("out")
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
        saved = sum(it["info"]["size"] - it["out"]["size"] for _, it in cand)
        msg = (f"원본 {len(cand)}개를 휴지통으로 보냅니다.\n확보 용량: {fmt_size(saved)}"
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
                          "status": "", "label": label}
                    dst = self.out_path(path)
                    if os.path.exists(dst):  # 이전에 만든 결과가 있으면 비교 대상으로 연결
                        it["out"] = {"path": dst, "size": os.path.getsize(dst)}
                        it["checked"] = False
                        it["status"] = "결과 있음"
                    else:
                        rec = self._was_bigger(path)
                        if rec:  # 예전에 인코딩했더니 커졌던 파일 → 기본 제외, 수동으로 체크하면 경고
                            it["bigger"] = rec
                            it["checked"] = False
                            it["status"] = f"이전에 {rec['pct']}% 커져서 제외"
                    iid = self.tree.insert("", "end")
                    self.items[iid] = it
                    self.refresh(iid)
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
                    self.msg.config(text=m[1])
                elif k == "scandone":
                    self.busy = False
                    self.apply_sort()
                    self.msg.config(text=f"목록 {len(self.items)}개 (스캔 {m[1]}개)")
                elif k == "status":
                    self.items[m[1]]["status"] = m[2]
                    if not m[2].startswith("인코딩 중"):
                        self.items[m[1]]["pct"] = 0
                    self.refresh(m[1])
                elif k == "prog":
                    self.items[m[1]]["status"] = f"인코딩 중 {m[2]:.0f}%"
                    self.items[m[1]]["pct"] = m[2]
                    self.refresh(m[1])
                    self.pb["value"] = m[3]
                elif k == "speed":  # 파일 하나 끝날 때마다 실측 속도를 이동평균으로 저장
                    old = self.speed.get(m[1])
                    self.speed[m[1]] = m[2] if not old else old * 0.5 + m[2] * 0.5
                    self._save_settings()
                    self.sched_summary()
                elif k == "log":
                    messagebox.showerror("인코딩 실패", f"{m[1]}\n\n{m[2][-1200:]}")
                elif k == "done":
                    self.busy = False
                    self.proc = None
                    self.btn_go.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.pb["value"] = 100 if m[1] == m[2] else self.pb["value"]
                    self.msg.config(text=f"끝: {m[1]}/{m[2]}개 완료")
                    if self.after_done.get() == AFTER[1] and not self.stop_flag:
                        self.confirm_shutdown()
        except queue.Empty:
            pass
        self.after(100, self._pump)


if __name__ == "__main__":
    App().mainloop()

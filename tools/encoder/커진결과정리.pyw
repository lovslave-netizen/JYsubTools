"""커진 결과 정리 도구.

폴더(하위 포함)를 훑어서 이 인코딩도구가 만든 결과 파일(h265_이름, 옛 빌드의 이름_h265)을
원본과 짝지어 크기를 비교하고, 결과가 원본보다 크거나 같은 것만 골라
확인 후 완전 삭제(휴지통 안 거침)한다. 원본은 건드리지 않는다.

이미 한참 전에 다른 설정으로 인코딩해 뒀거나, "커진 결과 자동 삭제" 기능이
없던 옛 빌드로 돌려서 커진 채로 남아 있는 파일들을 한꺼번에 정리하는 용도.
인코딩도구.pyw와는 별도 실행 파일이며, 설정/기록을 공유하지 않는다.
"""
import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

PREFIX = "h265_"
LEGACY_SUFFIX = "_h265"  # 초기 빌드가 쓰던 이름 규칙


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def find_original(result_path):
    """결과 파일 경로로부터 짝이 될 원본 경로를 추측. 못 찾으면 None."""
    d, name = os.path.split(result_path)
    base, ext = os.path.splitext(name)
    candidates = []
    if name.startswith(PREFIX):
        candidates.append(base[len(PREFIX):])
    if base.endswith(LEGACY_SUFFIX):
        candidates.append(base[:-len(LEGACY_SUFFIX)])
    for stem in candidates:
        for e in (ext, ".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".webm"):
            p = os.path.join(d, stem + e)
            if os.path.isfile(p) and os.path.normpath(p) != os.path.normpath(result_path):
                return p
    return None


def is_result_name(name):
    base = os.path.splitext(name)[0]
    return name.startswith(PREFIX) or base.endswith(LEGACY_SUFFIX)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("커진 결과 정리")
        self.geometry("980x560")
        self.q = queue.Queue()
        self.rows = {}  # iid -> dict(result, original, rsize, osize)
        self.busy = False
        self._ui()
        self.after(100, self._pump)

    def _ui(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="폴더 스캔 (하위 포함)", command=self.scan).pack(side="left")
        ttk.Button(top, text="전체 선택", command=lambda: self.set_all(True)).pack(side="left", padx=(12, 0))
        ttk.Button(top, text="전체 해제", command=lambda: self.set_all(False)).pack(side="left", padx=4)
        ttk.Label(top, text="(결과 크기 >= 원본 크기인 것만 목록에 나옴. 원본은 삭제하지 않음)").pack(
            side="left", padx=12)

        cols = ("sel", "name", "osize", "rsize", "pct")
        heads = ("선택", "결과 파일 (폴더)", "원본 크기", "결과 크기", "커진 비율")
        widths = (50, 560, 100, 100, 90)
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "name" else "center", stretch=(c == "name"))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="left", fill="y", pady=6)
        self.tree.bind("<Button-1>", self.on_click)

        bottom = ttk.Frame(self, padding=6)
        bottom.pack(side="bottom", fill="x", before=self.tree)
        self.msg = ttk.Label(bottom, text="폴더를 스캔하세요.")
        self.msg.pack(side="left")
        self.btn_del = ttk.Button(bottom, text="선택한 결과 완전 삭제", command=self.delete_selected)
        self.btn_del.pack(side="right")

    def scan(self):
        if self.busy:
            return
        d = filedialog.askdirectory(title="스캔할 폴더")
        if not d:
            return
        self.busy = True
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self._root = d
        self.msg.config(text="스캔 중...")

        def work():
            n = 0
            for root, _, names in os.walk(d):
                for name in names:
                    if not is_result_name(name):
                        continue
                    rpath = os.path.join(root, name)
                    opath = find_original(rpath)
                    if not opath:
                        continue
                    try:
                        rsize, osize = os.path.getsize(rpath), os.path.getsize(opath)
                    except OSError:
                        continue
                    n += 1
                    self.q.put(("scanning", n))
                    if rsize >= osize:
                        self.q.put(("row", rpath, opath, rsize, osize))
            self.q.put(("scandone", n))

        threading.Thread(target=work, daemon=True).start()

    def _row_values(self, row):
        pct = row["rsize"] / row["osize"] * 100 - 100
        label = os.path.relpath(row["result"], self._root)
        return ("☑" if row["checked"] else "☐", label, fmt_size(row["osize"]), fmt_size(row["rsize"]),
                f"+{pct:.0f}%")

    def refresh(self, iid):
        self.tree.item(iid, values=self._row_values(self.rows[iid]))

    def on_click(self, e):
        if self.tree.identify_column(e.x) == "#1":
            iid = self.tree.identify_row(e.y)
            if iid:
                self.rows[iid]["checked"] ^= True
                self.refresh(iid)
            return "break"

    def set_all(self, v):
        for iid in self.rows:
            self.rows[iid]["checked"] = v
            self.refresh(iid)

    def delete_selected(self):
        todo = [(iid, r) for iid, r in self.rows.items() if r["checked"]]
        if not todo:
            messagebox.showinfo("삭제", "선택된 항목이 없습니다.")
            return
        saved = sum(r["rsize"] for _, r in todo)
        if not messagebox.askyesno(
                "완전 삭제",
                f"결과 파일 {len(todo)}개를 완전 삭제합니다 (휴지통 안 거침). 원본은 그대로 둡니다.\n"
                f"확보 용량: {fmt_size(saved)}\n\n진행할까요?"):
            return
        fails = []
        for iid, r in todo:
            try:
                os.remove(r["result"])
                self.tree.delete(iid)
                del self.rows[iid]
            except OSError as e:
                fails.append(f"{os.path.basename(r['result'])}: {e}")
        msg = f"{len(todo) - len(fails)}개 삭제, {fmt_size(saved)} 확보"
        if fails:
            msg += f"\n실패 {len(fails)}개:\n" + "\n".join(fails[:10])
            messagebox.showwarning("일부 실패", msg)
        self.msg.config(text=msg.split("\n")[0])

    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "scanning":
                    self.msg.config(text=f"스캔 중... (결과 파일 {m[1]}개 확인)")
                elif m[0] == "row":
                    iid = self.tree.insert("", "end")
                    self.rows[iid] = {"result": m[1], "original": m[2], "rsize": m[3], "osize": m[4],
                                       "checked": True}
                    self.refresh(iid)
                elif m[0] == "scandone":
                    self.busy = False
                    self.msg.config(text=f"확인 {m[1]}개 중 커진 것 {len(self.rows)}개")
        except queue.Empty:
            pass
        self.after(100, self._pump)


if __name__ == "__main__":
    App().mainloop()

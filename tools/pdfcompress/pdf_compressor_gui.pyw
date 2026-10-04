"""
PDF 도구 모음 (GUI)
탭 3개:
  1) PDF 압축      - 폴더(하위 폴더 포함) 안의 모든 PDF를 압축
  2) PDF -> JPG    - PDF 여러 개를 각각 페이지별 JPG로 분할
  3) JPG -> PDF    - 폴더(하위 폴더 포함) 안의 이미지들을, 폴더 단위로 묶어 PDF 1개씩 생성

공통 동작: 원본은 건드리지 않고 "<선택폴더>_결과폴더" 같은 별도 폴더에 결과를 저장합니다.
           (PDF 압축 탭만 "원본 덮어쓰기" 옵션 있음)

필요 라이브러리: pymupdf, pillow
실행: pythonw pdf_compressor_gui.pyw  (더블클릭 실행, 콘솔창 안 뜸)
"""

import io
import os
import re
import shutil
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import fitz  # PyMuPDF
from PIL import Image

# ---------------------------------------------------------------- 공통 유틸

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")


def human_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- 공통 GUI 베이스

class BaseTab(ttk.Frame):
    """폴더 선택 + 옵션 + 시작 버튼 + 진행바 + 로그 를 갖춘 탭 공통 틀."""

    def __init__(self, parent, root):
        super().__init__(parent)
        self.root = root
        self.running = False
        self._build()

    def _build(self):
        pad = {"padx": 10, "pady": 6}

        frm_top = ttk.Frame(self)
        frm_top.pack(fill="x", **pad)
        ttk.Label(frm_top, text="대상 폴더:").pack(side="left")
        self.folder_var = tk.StringVar()
        ttk.Entry(frm_top, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(frm_top, text="찾아보기", command=self._browse_folder).pack(side="left")

        self.options_frame = ttk.Frame(self)
        self.options_frame.pack(fill="x", **pad)
        self.build_options(self.options_frame)

        frm_btn = ttk.Frame(self)
        frm_btn.pack(fill="x", **pad)
        self.start_btn = ttk.Button(frm_btn, text=self.start_label(), command=self.start)
        self.start_btn.pack(side="left")

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=10)

        frm_log = ttk.Frame(self)
        frm_log.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(frm_log, wrap="none", state="disabled")
        yscroll = ttk.Scrollbar(frm_log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

    # ---- 하위 클래스에서 구현 ----
    def build_options(self, frame):
        pass

    def start_label(self):
        return "시작"

    def collect_items(self, folder):
        """작업 대상 목록을 반환."""
        raise NotImplementedError

    def process_item(self, item, folder):
        """항목 하나 처리 후 로그 문자열 반환. 실패 시 예외 발생시키면 됨."""
        raise NotImplementedError

    def before_run(self, folder):
        """시작 전 확인/검증. 계속 진행하려면 True 반환."""
        return True

    def after_run(self):
        pass

    # ---- 공통 로직 ----
    def _browse_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.folder_var.set(path)

    def log_write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        if self.running:
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("오류", "올바른 폴더를 선택해 주세요.")
            return
        if not self.before_run(folder):
            return

        self.running = True
        self.start_btn.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress["value"] = 0

        t = threading.Thread(target=self._run_job, args=(folder,), daemon=True)
        t.start()

    def _run_job(self, folder):
        try:
            items = self.collect_items(folder)
        except Exception as e:
            self.root.after(0, lambda: self.log_write(f"오류: {e}"))
            self.root.after(0, self._finish)
            return

        total = len(items)
        self.root.after(0, lambda: self.progress.configure(maximum=max(total, 1)))
        self.root.after(0, lambda: self.status_var.set(f"{total}개 항목 발견"))

        if total == 0:
            self.root.after(0, lambda: self.log_write("처리할 항목이 없습니다."))
            self.root.after(0, self._finish)
            return

        ok = 0
        fail = 0
        for i, item in enumerate(items, 1):
            try:
                msg = self.process_item(item, folder)
                msg = f"[{i}/{total}] {msg}"
                ok += 1
            except Exception as e:
                msg = f"[{i}/{total}] 실패: {item} - {e}"
                fail += 1
                traceback.print_exc()
            self.root.after(0, lambda m=msg: self.log_write(m))
            self.root.after(0, lambda v=i: self.progress.configure(value=v))

        def summarize():
            self.log_write("")
            self.log_write(f"완료: 성공 {ok}개, 실패 {fail}개")
            self.status_var.set("완료")
            self.after_run()
            self._finish()

        self.root.after(0, summarize)

    def _finish(self):
        self.running = False
        self.start_btn.configure(state="normal")


# ---------------------------------------------------------------- 탭 1: PDF 압축

LEVELS = {
    "낮음 (화질 우선)": (85, 2400),
    "보통 (권장)": (65, 1800),
    "높음 (용량 우선)": (45, 1400),
}
MIN_IMAGE_PIXELS = 40_000  # 200x200 미만 이미지는 손대지 않음


def compress_pdf_bytes(src_path, quality, max_dim):
    doc = fitz.open(src_path)
    try:
        seen = set()
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    base = doc.extract_image(xref)
                    img_bytes = base["image"]
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    w, h = pil_img.size
                    if w * h < MIN_IMAGE_PIXELS:
                        continue

                    resized = False
                    if max(w, h) > max_dim:
                        scale = max_dim / max(w, h)
                        pil_img = pil_img.resize(
                            (max(1, int(w * scale)), max(1, int(h * scale))),
                            Image.LANCZOS,
                        )
                        resized = True

                    if pil_img.mode in ("RGBA", "P", "CMYK"):
                        pil_img = pil_img.convert("RGB")

                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=quality, optimize=True)
                    new_bytes = buf.getvalue()

                    if resized or len(new_bytes) < len(img_bytes):
                        page.replace_image(xref, stream=new_bytes)
                except Exception:
                    continue

        return doc.tobytes(
            garbage=4, deflate=True, clean=True,
            deflate_images=True, deflate_fonts=True,
        )
    finally:
        doc.close()


class CompressTab(BaseTab):
    def build_options(self, frame):
        ttk.Label(frame, text="압축 강도:").pack(side="left")
        self.level_var = tk.StringVar(value="보통 (권장)")
        ttk.Combobox(
            frame, textvariable=self.level_var, values=list(LEVELS.keys()),
            state="readonly", width=16,
        ).pack(side="left", padx=6)

        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="원본 파일 덮어쓰기 (되돌릴 수 없음, 주의)",
            variable=self.overwrite_var,
        ).pack(side="left", padx=20)

    def start_label(self):
        return "압축 시작"

    def before_run(self, folder):
        if self.overwrite_var.get():
            return messagebox.askyesno(
                "확인", "원본 파일을 압축본으로 덮어씁니다. 되돌릴 수 없습니다.\n계속할까요?"
            )
        return True

    def collect_items(self, folder):
        out_root = folder.rstrip("\\/") + "_압축"
        overwrite = self.overwrite_var.get()
        items = []
        for dirpath, _dirnames, filenames in os.walk(folder):
            if not overwrite and os.path.abspath(dirpath).startswith(os.path.abspath(out_root)):
                continue
            for fn in filenames:
                if fn.lower().endswith(".pdf"):
                    items.append(os.path.join(dirpath, fn))
        self._out_root = out_root
        self._total_before = 0
        self._total_after = 0
        return items

    def process_item(self, src, folder):
        quality, max_dim = LEVELS[self.level_var.get()]
        overwrite = self.overwrite_var.get()
        rel = os.path.relpath(src, folder)

        before_size = os.path.getsize(src)
        new_bytes = compress_pdf_bytes(src, quality, max_dim)
        after_size = len(new_bytes)

        if after_size >= before_size:
            after_size = before_size
            result_bytes = None
        else:
            result_bytes = new_bytes

        if overwrite:
            if result_bytes is not None:
                tmp_path = src + ".tmp_compress"
                with open(tmp_path, "wb") as f:
                    f.write(result_bytes)
                os.replace(tmp_path, src)
        else:
            dst = os.path.join(self._out_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if result_bytes is not None:
                with open(dst, "wb") as f:
                    f.write(result_bytes)
            else:
                shutil.copy2(src, dst)

        self._total_before += before_size
        self._total_after += after_size

        pct = 0 if before_size == 0 else (1 - after_size / before_size) * 100
        return f"{rel}  {human_size(before_size)} -> {human_size(after_size)} ({pct:.0f}% 감소)"

    def after_run(self):
        saved_pct = 0 if self._total_before == 0 else (1 - self._total_after / self._total_before) * 100
        self.log_write(
            f"전체 용량: {human_size(self._total_before)} -> {human_size(self._total_after)} "
            f"({saved_pct:.0f}% 절감)"
        )
        if not self.overwrite_var.get():
            self.log_write(f"결과 저장 위치: {self._out_root}")


# ---------------------------------------------------------------- 탭 2: PDF -> JPG

class PdfToJpgTab(BaseTab):
    def build_options(self, frame):
        ttk.Label(frame, text="해상도(DPI):").pack(side="left")
        self.dpi_var = tk.StringVar(value="200")
        ttk.Combobox(
            frame, textvariable=self.dpi_var, values=["100", "150", "200", "300"],
            state="readonly", width=8,
        ).pack(side="left", padx=6)

        ttk.Label(frame, text="화질:").pack(side="left", padx=(20, 0))
        self.quality_var = tk.StringVar(value="85")
        ttk.Combobox(
            frame, textvariable=self.quality_var, values=["60", "75", "85", "95"],
            state="readonly", width=8,
        ).pack(side="left", padx=6)

    def start_label(self):
        return "JPG로 변환 시작"

    def collect_items(self, folder):
        out_root = folder.rstrip("\\/") + "_JPG변환"
        items = []
        for dirpath, _dirnames, filenames in os.walk(folder):
            if os.path.abspath(dirpath).startswith(os.path.abspath(out_root)):
                continue
            for fn in filenames:
                if fn.lower().endswith(".pdf"):
                    items.append(os.path.join(dirpath, fn))
        self._out_root = out_root
        return items

    def process_item(self, src, folder):
        dpi = int(self.dpi_var.get())
        quality = int(self.quality_var.get())
        rel = os.path.relpath(src, folder)
        rel_noext, _ = os.path.splitext(rel)
        out_dir = os.path.join(self._out_root, rel_noext)
        os.makedirs(out_dir, exist_ok=True)

        doc = fitz.open(src)
        try:
            count = doc.page_count
            digits = max(3, len(str(count)))
            for i, page in enumerate(doc, 1):
                pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                out_path = os.path.join(out_dir, f"{i:0{digits}d}.jpg")
                img.save(out_path, format="JPEG", quality=quality)
        finally:
            doc.close()

        return f"{rel}  {count}페이지 -> {out_dir}"

    def after_run(self):
        self.log_write(f"결과 저장 위치: {self._out_root}")


# ---------------------------------------------------------------- 탭 3: JPG -> PDF

class JpgToPdfTab(BaseTab):
    def build_options(self, frame):
        ttk.Label(
            frame,
            text="같은 폴더 안 이미지들을 파일명 순서로 묶어 폴더당 PDF 1개를 만듭니다.",
        ).pack(side="left")

    def start_label(self):
        return "PDF로 묶기 시작"

    def collect_items(self, folder):
        out_root = folder.rstrip("\\/") + "_PDF변환"
        groups = []
        for dirpath, _dirnames, filenames in os.walk(folder):
            if os.path.abspath(dirpath).startswith(os.path.abspath(out_root)):
                continue
            images = sorted(
                (fn for fn in filenames if fn.lower().endswith(IMAGE_EXTS)),
                key=natural_key,
            )
            if images:
                groups.append((dirpath, images))
        self._out_root = out_root
        return groups

    def process_item(self, item, folder):
        dirpath, images = item
        rel = os.path.relpath(dirpath, folder)

        if rel == ".":
            out_name = os.path.basename(folder.rstrip("\\/")) + ".pdf"
            out_path = os.path.join(self._out_root, out_name)
        else:
            out_path = os.path.join(self._out_root, rel + ".pdf")

        os.makedirs(os.path.dirname(out_path) or self._out_root, exist_ok=True)

        pil_images = []
        for fn in images:
            im = Image.open(os.path.join(dirpath, fn))
            if im.mode != "RGB":
                im = im.convert("RGB")
            pil_images.append(im)

        first, rest = pil_images[0], pil_images[1:]
        first.save(out_path, save_all=True, append_images=rest)

        rel_disp = rel if rel != "." else os.path.basename(folder.rstrip("\\/"))
        return f"{rel_disp}  이미지 {len(images)}장 -> {os.path.basename(out_path)}"

    def after_run(self):
        self.log_write(f"결과 저장 위치: {self._out_root}")


# ---------------------------------------------------------------- 메인

if __name__ == "__main__":
    root = tk.Tk()
    root.title("PDF 도구 모음")
    root.geometry("760x560")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    tab1 = CompressTab(notebook, root)
    tab2 = PdfToJpgTab(notebook, root)
    tab3 = JpgToPdfTab(notebook, root)

    notebook.add(tab1, text="PDF 압축")
    notebook.add(tab2, text="PDF → JPG")
    notebook.add(tab3, text="JPG → PDF")

    root.mainloop()

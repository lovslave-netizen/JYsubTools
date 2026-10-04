"""JYsubTools 빌드: tools.json 을 읽어 (1) 툴별 PyInstaller 빌드 (2) Inno Setup 스크립트 생성 (3) setup.exe 컴파일.

사용: python scripts/build.py [--skip-tools] [--no-compile]
  --skip-tools  PyInstaller 단계를 건너뜀 (이미 빌드한 stage 재사용, setup 스크립트만 다시 만들 때)
작업 폴더(JYSUB_BUILD, 기본 %LOCALAPPDATA%\\JYsubToolsBuild)는 로컬 드라이브여야 한다
(네트워크 드라이브에서는 exe 실행이 막힐 수 있음). 결과 setup 은 repo\\dist 에 생긴다.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.environ.get("JYSUB_BUILD") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "JYsubToolsBuild")
STAGE = os.path.join(BUILD, "stage")
WORK = os.path.join(BUILD, "work")
CACHE = os.path.join(BUILD, "cache")
OUT = os.path.join(ROOT, "dist")
SEVENZR = ("https://www.7-zip.org/a/7zr.exe",
           "ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(url, dest, want):
    if os.path.exists(dest) and sha256(dest) == want:
        return
    print("  download", url)
    req = urllib.request.Request(url, headers={"User-Agent": "JYsubTools-build"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if sha256(dest) != want:
        raise SystemExit(f"sha256 불일치: {dest}")


def build_tools(cfg):
    shutil.rmtree(STAGE, ignore_errors=True)
    for t in cfg["tools"]:
        print("== PyInstaller:", t["id"])
        wd = os.path.join(WORK, t["id"])
        shutil.rmtree(wd, ignore_errors=True)
        os.makedirs(wd)
        entry = os.path.join(wd, "main_%s.py" % t["id"])   # 한글/.pyw 파일명 문제를 피하려고 복사해서 빌드
        shutil.copyfile(os.path.join(ROOT, t["entry"]), entry)
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
               "--name", t["exe"], "--distpath", os.path.join(STAGE, t["id"]),
               "--workpath", os.path.join(wd, "w"), "--specpath", wd]
        for m in t.get("excludes", []):
            cmd += ["--exclude-module", m]
        subprocess.run(cmd + [entry], check=True)


def pas(s):
    return "'" + s.replace("'", "''") + "'"


def make_iss(cfg):
    tools, deps = cfg["tools"], cfg["deps"]
    ver = cfg["version"]
    L = []
    L.append(f"""[Setup]
AppId={{{{{cfg['app_id']}}}
AppName=JYsubTools
AppVersion={ver}
AppPublisher=JY
DefaultDirName={{localappdata}}\\Programs\\JYsubTools
DefaultGroupName=JYsubTools
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir={OUT}
OutputBaseFilename=JYsubTools-Setup-{ver}
UninstallDisplayName=JYsubTools

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\\Korean.isl"

[Types]
Name: "full"; Description: "전체 설치"
Name: "custom"; Description: "선택 설치"; Flags: iscustom

[Components]""")
    for t in tools:
        need = ", ".join(deps[d]["label"] for d in t["deps"])
        desc = t["name"] + " - " + t["desc"] + (f"  [자동 설치: {need}]" if need else "")
        L.append(f'Name: "{t["id"]}"; Description: "{desc}"; Types: full custom')
    L.append("\n[Files]")
    for t in tools:
        src = os.path.join(STAGE, t["id"], t["exe"])
        L.append(f'Source: "{src}\\*"; DestDir: "{{app}}\\{t["id"]}"; Flags: recursesubdirs createallsubdirs ignoreversion; Components: {t["id"]}')
    L.append(f'Source: "{os.path.join(CACHE, "7zr.exe")}"; DestDir: "{{tmp}}"; Flags: deleteafterinstall')
    L.append("\n[Icons]")
    for t in tools:
        L.append(f'Name: "{{group}}\\{t["name"]}"; Filename: "{{app}}\\{t["id"]}\\{t["exe"]}.exe"; Components: {t["id"]}')

    # ---- Pascal 코드: 선택된 툴에서 필요한 의존성만 받아서 {app}\bin 에 푼다 ----
    def need_expr(dep):
        ids = [t["id"] for t in tools if dep in t["deps"]]
        return " or ".join(f"WizardIsComponentSelected({pas(i)})" for i in ids) or "False"

    code = ["\n[Code]", "var", "  DownloadPage: TDownloadWizardPage;", "  DepFailures: String;", "",
            "function DepNeeded(const Dep: String): Boolean;", "begin", "  Result := False;"]
    for d in deps:
        code.append(f"  if Dep = {pas(d)} then Result := {need_expr(d)};")
    code += ["end;", "",
             "function DepMarker(const Dep, Ver: String): String;", "begin",
             "  Result := ExpandConstant('{app}\\bin\\.dep_') + Dep + '_' + Ver;", "end;", "",
             "function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;",
             "begin", "  if Progress = ProgressMax then Log('다운로드 완료: ' + FileName);", "  Result := True;", "end;", "",
             "procedure InitializeWizard;", "begin",
             "  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @OnDownloadProgress);",
             "end;", "",
             "procedure QueueDep(const Dep, Url, FileName, Sha, Ver: String);", "var Offline: String;", "begin",
             "  if not DepNeeded(Dep) then Exit;",
             "  if FileExists(DepMarker(Dep, Ver)) then Exit;",
             "  Offline := ExpandConstant('{src}\\deps\\') + FileName;",
             "  if FileExists(Offline) then begin",
             "    FileCopy(Offline, ExpandConstant('{tmp}\\') + FileName, False);",
             "    Exit;",
             "  end;",
             "  DownloadPage.Add(Url, FileName, Sha);",
             "end;", "",
             "function PrepareToInstall(var NeedsRestart: Boolean): String;", "begin",
             "  Result := '';",
             "  DownloadPage.Clear;"]
    for n, d in deps.items():
        code.append(f"  QueueDep({pas(n)}, {pas(d['url'])}, {pas(d['file'])}, {pas(d['sha256'])}, {pas(d['version'])});")
    code += ["  DownloadPage.Show;", "  try", "    try",
             "      DownloadPage.Download;", "    except",
             "      Result := '필요한 파일을 받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.' + #13#10 + GetExceptionMessage;",
             "    end;", "  finally", "    DownloadPage.Hide;", "  end;", "end;", "",
             "procedure ExtractDep(const Dep, FileName, Ver: String; const Picks: array of String);",
             "var Rc, I: Integer; Dest, Arc: String;", "begin",
             "  if not DepNeeded(Dep) then Exit;",
             "  if FileExists(DepMarker(Dep, Ver)) then Exit;",
             "  Arc := ExpandConstant('{tmp}\\') + FileName;",
             "  Dest := ExpandConstant('{tmp}\\x_') + Dep;",
             "  ForceDirectories(Dest);",
             "  if (not FileExists(Arc)) or (not Exec(ExpandConstant('{tmp}\\7zr.exe'), 'x \"' + Arc + '\" -o\"' + Dest + '\" -y', '', SW_HIDE, ewWaitUntilTerminated, Rc)) or (Rc <> 0) then begin",
             "    DepFailures := DepFailures + #13#10 + Dep;",
             "    Exit;",
             "  end;",
             "  ForceDirectories(ExpandConstant('{app}\\bin'));",
             "  for I := 0 to GetArrayLength(Picks) - 1 do",
             "    if not FileCopy(Dest + '\\' + Picks[I], ExpandConstant('{app}\\bin\\') + ExtractFileName(Picks[I]), False) then",
             "      DepFailures := DepFailures + #13#10 + Dep + ': ' + Picks[I];",
             "  SaveStringToFile(DepMarker(Dep, Ver), Ver, False);",
             "  DelTree(Dest, True, True, True);",
             "end;", "",
             "procedure CurStepChanged(CurStep: TSetupStep);", "begin",
             "  if CurStep = ssPostInstall then begin"]
    for n, d in deps.items():
        picks = "[" + ", ".join(pas(p) for p in d["pick"]) + "]"
        code.append(f"    ExtractDep({pas(n)}, {pas(d['file'])}, {pas(d['version'])}, {picks});")
    code += ["    if DepFailures <> '' then",
             "      MsgBox('다음 구성요소를 설치하지 못했습니다:' + DepFailures + #13#10 + 'setup 을 다시 실행하면 다시 시도합니다.', mbError, MB_OK);",
             "  end;", "end;"]
    path = os.path.join(BUILD, "JYsubTools.iss")
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n".join(L + code) + "\n")
    return path


def find_iscc():
    for base in (os.environ.get("LOCALAPPDATA", "") + r"\Programs\Inno Setup 6",
                 os.environ.get("ProgramFiles(x86)", "") + r"\Inno Setup 6",
                 os.environ.get("ProgramFiles", "") + r"\Inno Setup 6"):
        p = os.path.join(base, "ISCC.exe")
        if os.path.exists(p):
            return p
    return shutil.which("ISCC") or sys.exit("Inno Setup 6 (ISCC.exe) 를 찾을 수 없습니다")


def main():
    cfg = json.load(open(os.path.join(ROOT, "tools.json"), encoding="utf-8"))
    os.makedirs(CACHE, exist_ok=True)
    fetch(SEVENZR[0], os.path.join(CACHE, "7zr.exe"), SEVENZR[1])
    if "--skip-tools" not in sys.argv:
        build_tools(cfg)
    iss = make_iss(cfg)
    print("iss:", iss)
    if "--no-compile" in sys.argv:
        return
    os.makedirs(OUT, exist_ok=True)
    subprocess.run([find_iscc(), iss], check=True)
    print("완료:", os.path.join(OUT, "JYsubTools-Setup-%s.exe" % cfg["version"]))


if __name__ == "__main__":
    main()

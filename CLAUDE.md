# JYsubTools — 이어서 작업할 때

setup 하나로 개인 툴(인코딩도구, JYPlayer, PDF압축기 등)을 체크박스로 골라 설치하는 묶음. 비공개 저장소 `lovslave-netizen/JYsubTools`. 진행 기록은 `진행상황.md`.

## 구조
- `tools/<id>/` 툴 소스 (py/pyw). 새 툴 추가 = 폴더 + `tools.json` 의 tools 항목 한 개(`deps` 에 필요한 의존성 이름).
- `tools.json` 이 기준: 버전, 툴 목록, 의존성(ffmpeg, libmpv)의 URL/sha256/설치할 파일.
- `scripts/build.py` : tools.json → 툴별 PyInstaller(onedir) → Inno Setup 스크립트 생성 → setup 컴파일. 작업 폴더는 로컬(`%LOCALAPPDATA%\JYsubToolsBuild`), 결과는 `dist\`.
- 설치 폴더 `%LOCALAPPDATA%\Programs\JYsubTools\<id>\` + 공용 `bin\`(ffmpeg, ffprobe, libmpv-2.dll). 툴은 `exe\..\bin` 을 먼저 찾는다.
- 의존성은 git 에 넣지 않는다(100MB 제한). setup 이 **설치할 때** 선택된 툴에 필요한 것만 받아서(sha256 검증) bin 에 푼다. setup 옆 `deps\` 폴더에 같은 이름의 7z 가 있으면 다운로드 대신 사용.
- 의존성 주소는 외부 공개 배포처(GyanD/codexffmpeg, shinchiro/mpv-winbuild-cmake). 배포처에서 파일이 사라지면 tools.json 의 URL/해시를 새 버전으로 교체.

## 작업 규칙
- 배포(커밋/push/태그)는 사용자가 "올려"라고 할 때만. 올릴 때 exe/setup 구성은 이미 정해졌으므로 따로 묻지 않는다.
- 릴리스: `.\scripts\release.ps1 <버전> "<요약>"` (tools.json 버전 → 커밋 → 태그 → Actions 빌드 → Setup\ 로 다운로드).
- W: 드라이브에서는 exe 실행이 막힘(Access denied) → 빌드/테스트는 로컬 폴더에서. git 은 W: 저장소를 safe.directory 로 등록해 사용.
- 파일 편집 후 내용이 0바이트로 비워진 적이 있음(Edit 후크). 편집 뒤 파일 크기를 확인할 것.
- 테스트 설치는 임시 폴더에 `/VERYSILENT /NOICONS /DIR=...` 로 하고 끝나면 언인스톨. 사용자의 실행 중인 프로세스는 건드리지 않는다.
- 한국어로 답하고, 확인 못 한 것은 확인 안 했다고 말한다.

## git 위치 (이 PC)
- W: 에서 git 인덱스가 0바이트로 깨지는 일이 있어(덮어쓰기 쓰기 불안정) git 데이터는 `C:\Users\lovsl\.jysubtools-git` 에 있다. 작업 폴더는 W:\파이썬\JYsubTools. git 명령 전에 `GIT_DIR='C:\Users\lovsl\.jysubtools-git'; GIT_WORK_TREE='W:\파이썬\JYsubTools'` 환경변수를 지정할 것. (W: 쪽 빈 `.git` 폴더는 삭제 대기 상태로 남아 있음)
- 다른 PC 에서는 GitHub 에서 `git clone` 해서 쓰면 된다.

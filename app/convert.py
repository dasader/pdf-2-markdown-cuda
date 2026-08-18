import hashlib
import html
import os
import re
import zipfile
from functools import lru_cache
from pathlib import Path

import pypdfium2  # docling이 이미 의존하는 PDF 백엔드


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


# 텍스트 레이어 표본 페이지 수. 앞뒤 표지·간지는 원래 비어 있으므로 문서 전체에
# 고르게 흩어 뽑는다. 13페이지 안팎이면 500페이지 문서에서도 수십 ms.
_TEXT_PROBE_PAGES = 12


def probe(path) -> tuple[int, int]:
    """(페이지 수, 표본 페이지의 텍스트 길이). 빈 PDF·스캔본 판별용.

    do_ocr=False인 이 파이프라인에서 스캔본(이미지) PDF는 예외 없이 '성공'하고
    텅 빈 doc.md를 돌려준다. 업로드 시점에 걸러야 사용자가 몇 분을 기다린 끝에
    빈 결과를 받는 일이 없다.
    """
    doc = pypdfium2.PdfDocument(path)
    try:
        n = len(doc)
        if not n:
            return 0, 0
        step = max(1, n // _TEXT_PROBE_PAGES)
        chars = sum(len(doc[i].get_textpage().get_text_bounded().strip())
                    for i in range(0, n, step))
        return n, chars
    finally:
        doc.close()


# 변환 결과물이 달라지는 변경을 하면 올린다. opts_hash에 섞이므로 find_cached가 옛
# 결과를 더는 찾지 못해 캐시가 자연히 무효화된다(수동 삭제 불필요).
#   rev 2: docling 기본 백엔드로 복귀(pypdfium 백엔드가 한글 음절을 중복 삽입) +
#          마크다운 HTML 언이스케이프
#   rev 3: generate_picture_images 복구 — rev 2 캐시에는 그림이 없다
#   rev 4: 표 CSV를 utf-8-sig로 저장(Excel 한글 깨짐) + 공문서 불릿 기호를 들여쓰기로
#   rev 5: PDF 자간(letter-spacing)으로 음절이 벌어진 텍스트 되붙이기("글 로 벌"→"글로벌")
#          + 구두점 주변 과잉 공백 정리("산 · 학 · 연"→"산·학·연", "( 연 )"→"(연)")
#          + 심볼폰트 불릿 'l'(▪) 정리("- l 내용"→"- 내용")
#   rev 6: 제목 규칙(번호/제목이 갈린 제목 병합, 번호·기호로 계층 복원) + 불릿 규칙
#          (◇◆▷▶·soft hyphen·¡Ÿ 기호 추가, 반복 기호, 기호 없는 항목은 자식으로,
#          각주/비고 줄은 불릿 해제, 잘린 항목 이어붙이기, 목록 깊이 정규화) +
#          PUA 글리프 제거 + 어절 자간 잔재("체계성  -  부처") 정리
#   rev 7: docling-parse 7.8.1 — 붙어 나오던 한글 어절이 풀리고 목차 표의 쪽번호가
#          제 셀로 들어간다. 본문이 달라지므로 7.7.0으로 만든 캐시는 버려야 한다.
CONVERTER_REV = 7


def opts_hash(include_images: bool, include_tables_csv: bool) -> str:
    key = f"rev={CONVERTER_REV};img={int(include_images)};csv={int(include_tables_csv)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# 공문서 불릿 기호 → 목록 깊이. docling은 이 기호를 본문 글자로 남기므로 "- ㅇ 내용"
# 처럼 불릿이 겹쳐 보인다. 기호를 지우고 그 계층을 들여쓰기로 옮긴다.
# ①②③·⇨는 순번·지시 정보를 담고 있어 기호를 지우면 뜻이 사라진다 — 목록에서 제외.
# ­(soft hyphen): 공문서 하위 불릿 '-'가 이 코드포인트로 조판돼 그대로 추출된다.
# 눈에는 보통 하이픈과 같아 보이지만 마크다운에는 보이지 않는 글자로 남는다.
# ¡(U+00A1)·Ÿ(U+0178): 심볼폰트 불릿이 라틴 글자로 추출된 것. 한글 공문서에 이 글자가
# 본문으로 나올 일은 없다(실측: 한 문서에서 각각 228개, 26개).
_BULLET_DEPTH = {"□": 0, "ㅁ": 0, "■": 0, "◇": 0, "◆": 0,
                 "ㅇ": 1, "○": 1, "◦": 1, "●": 1, "▷": 1, "▶": 1, "¡": 1, "Ÿ": 1,
                 "▪": 2, "-": 2, "­": 2}
# re.escape: 기호 목록에 '-'가 섞여 있어 문자클래스에서 범위로 읽히면 안 된다.
_BULLET_CLASS = f"[{re.escape(''.join(_BULLET_DEPTH))}]"
# docling이 이미 들여쓴 줄도 받고, 같은 기호가 두 번 찍힌 조판도 벗긴다(실측 페이지
# 머리말: "- ▪ ▪   2025년도 …"). 반복은 공백으로 갈렸을 때만 인정해야 "- ㅇ ㅇㅇ은행"의
# 본문 첫 글자를 기호로 먹지 않는다.
_BULLET_RE = re.compile(rf"^ *- ({_BULLET_CLASS})(?:(?: +\1)+ +| *)(.*)$")
# 내용 없이 기호만 남은 줄. docling이 빈 체크박스("- [] ¡")로 내보내는 서식도 포함.
_BARE_SYMBOL_RE = re.compile(rf"^ *(?:- )?(?:\[.?\] )?{_BULLET_CLASS} *$")
# 심볼폰트 잔재 중 목록으로 못 바꾸는 것(표 셀 안 등)은 눈에 보이는 불릿으로만 바꾼다.
_LEFTOVER_SYMBOLS = ("¡", "Ÿ")
# 사유 영역(PUA) 글리프: 심볼폰트 문자가 매핑 없이 그대로 나온 것. 뜻이 없으니 지운다.
_PUA_RE = re.compile("[-] *")

# 심볼폰트 불릿(Wingdings 'l'=속 채운 사각 ▪)이 본문 글자 'l'로 추출돼 "- l 내용"으로
# 나온다(공문서 하위 불릿에 흔함). 반드시 뒤 공백/줄끝을 요구해 실제 'l'로 시작하는
# 낱말("- long term ...")을 오검하지 않는다. 부모 없는 최상위라 0단계로 둔다.
_LBULLET_RE = re.compile(r"^- l(?: (.*))?$")

# 순번·지시 기호로 시작하는 항목은 기호가 소실된 하위 항목이 아니라 그 자체로 온전한
# 형제 항목이다 — 아래 자식 승격 규칙에서 제외한다.
_ORDINAL_RE = re.compile(r"^[①-⑳→⇒⇨]")


# 각주(*, **)·비고(※)로 시작하는 줄은 목록 항목이 아니라 바로 위 내용의 주석이다.
# docling이 "- ※ …"처럼 불릿으로 승격시키면 본문 항목과 구분이 사라지고, "* …"는
# 마크다운이 불릿으로 렌더해 각주 표시가 통째로 사라진다. 불릿을 떼고 '*'는
# 이스케이프해 글자 그대로 남긴다.
_NOTE_RE = re.compile(r"^(?:- )?(\*{1,2}|※)\s*(?=[^\s*])")


def _as_note(line: str):
    m = _NOTE_RE.match(line)
    if not m:
        return None
    rest = line[m.end():]
    if "*" in m.group(1) and "*" in rest:
        return None                         # "**강조** …" 같은 인라인 서식 — 각주가 아니다
    return m.group(1).replace("*", r"\*") + " " + rest


def _fix_bullets(md: str) -> str:
    out, depth = [], None                   # depth: 직전 기호 항목의 깊이
    for line in md.split("\n"):
        m = _BULLET_RE.match(line)          # 표 행은 '|'로 시작해 매칭되지 않는다
        if (note := _as_note(line)) is not None:
            # 빈 줄이 없으면 마크다운이 각주를 바로 위 목록 항목의 이어진 문장으로
            # 삼켜(lazy continuation) 줄바꿈이 사라진다. 각주는 목록을 끊지 않는다.
            if out and out[-1].strip():
                out.append("")
            line = note
        elif m:
            text, depth = (m.group(2) or "").strip(), _BULLET_DEPTH[m.group(1)]
            if not text:
                depth -= 1                  # 본문은 다음 줄 — 그 줄이 이 기호의 자리를 차지
                continue
            line = "  " * depth + "- " + text
        elif _BARE_SYMBOL_RE.match(line):
            continue
        elif (lm := _LBULLET_RE.match(line)):
            text, depth = (lm.group(1) or "").strip(), 0
            if not text:
                depth = -1
                continue
            line = "- " + text
        elif ((li := _LI_RE.match(line)) and depth is not None
                and not _ORDINAL_RE.match(li.group(2))):
            # 기호 없는 항목 = 직전 기호 항목의 하위. docling은 원문의 하위 불릿('-')을
            # 마크다운 불릿으로 흡수해 기호를 지워버리므로, 들여쓰기만 믿으면 부모와
            # 자식이 뒤집힌다("¡ (위원회 체계)" 밑의 "- R&D 자체평가위원회는 …").
            line = "  " * (depth + 1) + "- " + li.group(2)
        elif line.strip():
            depth = None                    # 목록이 아닌 내용 = 목록 블록의 끝
        out.append(line)
    return "\n".join(out)


# 공문서는 제목 번호를 본문과 다른 텍스트 상자에 찍는다. docling은 그걸 별개 블록으로
# 읽어 "## 2"와 "## 평가방향"을 두 개의 제목으로 내보낸다. 번호만 있는 제목은 뒤따르는
# 제목에 합친다.
_HEAD_RE = re.compile(r"^(#{1,6}) +(\S.*?)\s*$")
_NUM_ONLY = re.compile(r"^(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|\d+(?:[-–.]\d+)*\.?|[가-힣][.)])$")


def _merge_split_headings(lines: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(lines):
        m = _HEAD_RE.match(lines[i])
        if m and _NUM_ONLY.match(m.group(2)):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1                      # 번호와 제목 사이 빈 줄 건너뛰기
            nxt = _HEAD_RE.match(lines[j]) if j < len(lines) else None
            if nxt and not _NUM_ONLY.match(nxt.group(2)):
                out.append(f"{m.group(1)} {m.group(2)} {nxt.group(2)}")
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return out


# docling은 PDF의 시각적 줄바꿈을 그대로 항목 경계로 삼아, 한 문장을 두 개의 불릿으로
# 쪼갠다("… 사업전략 수정," / "추진체계 개편 등을 …"). 앞 항목이 쉼표·가운뎃점·접속사·
# 단독 조사로 끝나면 문장이 안 끝난 것이므로 다음 항목을 이어붙인다. 종결어미로 끝나는
# 정상 항목은 건드리지 않는다.
# ponytail: "…한다"·"…부처의" 뒤에서 잘린 줄은 못 잡는다. 끝 음절이 조사와 같은 명사
# ("성과", "결과", "정의")가 흔해 조사 판별에 형태소 분석이 필요하고, 오검(멀쩡한 두
# 항목을 붙임)이 놓침보다 훨씬 나쁘다. 어절 단위로 확실한 신호만 쓴다.
_UNFINISHED = re.compile(
    r"(?:[,，·･]|적인|하여|되어|(?<![가-힣])(?:및|또는|혹은|그리고|위한|대한|관한|따른|의한|통한"
    r"|이|가|을|를|의|에|와|과|로|으로))$")
# 뒷줄의 시작 신호가 앞줄의 끝 신호보다 안전하다 — 한국어 문장은 어미("하여")나
# 조사·접속사("등", "및")로 시작할 수 없다. 잘린 어절("명문화" / "하여 …")도 잡힌다.
_CONTINUES = re.compile(r"^(?:하여|하고|하는|한다|되어|되고|되는|된다|(?:등|및|또는)(?=\s|$))")
_LI_RE = re.compile(r"^( *)- (\S.*)$")


def _join_wrapped(lines: list[str]) -> list[str]:
    """들여쓰기가 달라도 잇는다 — docling은 잘린 뒷줄을 최상위 항목으로 내보내는 일이
    잦다("  - … 정책관점 혹은" / "- 정책수단에 대한 …"). 자식 항목을 거느린 부모가
    쉼표·접속사로 끝나는 일은 없으므로 깊이를 조건에 넣을 이유가 없다."""
    out = []
    for line in lines:
        m = _LI_RE.match(line)              # 표 행('|')·제목('#')은 매칭되지 않는다
        prev = _LI_RE.match(out[-1]) if out else None
        if (m and prev
                and (_UNFINISHED.search(prev.group(2)) or _CONTINUES.match(m.group(2)))):
            out[-1] += " " + m.group(2)
            continue
        out.append(line)
    return out


def _normalize_depths(lines: list[str]) -> list[str]:
    """목록 블록마다 깊이를 0부터 다시 매긴다.

    마크다운에서 부모 없는 4칸 들여쓰기는 목록이 아니라 **코드 블록**이다. docling은
    PDF의 시각적 여백을 그대로 들여쓰기로 옮겨(제목 바로 밑에 4칸 항목) 본문 한 단락을
    통째로 코드 블록으로 렌더시킨다. 상대 깊이만 살리고 절대값은 버린다.
    """
    out, stack = [], []
    for line in lines:
        m = _LI_RE.match(line)
        if not m:
            if line.strip():
                stack = []                  # 목록이 아닌 내용 = 목록 블록의 끝
            out.append(line)
            continue
        indent = len(m.group(1))
        while stack and indent < stack[-1]:
            stack.pop()
        if not stack or indent > stack[-1]:
            stack.append(indent)
        out.append("  " * (len(stack) - 1) + "- " + m.group(2))
    return out


# 자간 잔재: 강조 조판에서 어절 사이가 두 칸 이상으로 남은 것을 한 칸으로 접는다.
# _despace가 두 칸을 어절 경계 표식으로 쓰므로 반드시 그 뒤에 돌린다. 들여쓰기(목록
# 깊이)는 (?<=\S)로 보존하고, 표 행은 셀 정렬이 깨지지 않게 통째로 건너뛴다.
_INNER_SPACES = re.compile(r"(?<=\S) {2,}")


def _collapse_spaces(lines: list[str]) -> list[str]:
    return [ln if ln.lstrip().startswith("|") else _INNER_SPACES.sub(" ", ln) for ln in lines]


# 공문서 목차 번호 → 제목 깊이. docling은 본문 제목을 전부 같은 레벨(##)로 내보내
# 문서 계층이 통째로 사라진다. 공문서는 번호 형식이 곧 계층이므로 그것으로 되살린다.
_HEAD_LEVELS = (
    (re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]"), 1),          # Ⅰ 배경 및 방향
    (re.compile(r"^\d+ *[-–] *\d+"), 3),           # 1-3. 체계성
    (re.compile(r"^[가-힣][.)] "), 3),              # 가. 세부항목
    (re.compile(r"^(?:제 *)?\d+[.장절]?(?: |$)"), 2),  # 1. 평가배경 / 2 평가방향
)
# 제목 앞에 붙은 공문서 불릿 기호("■ 특정평가 목적")도 계층 신호다. 목록에서 쓰는
# 깊이를 그대로 재활용한다(□■◇→h2, ㅇ○▷¡→h3, ▪→h4).
_HEAD_SYMBOL = re.compile(rf"^({_BULLET_CLASS})\s*(\S.*)$")


def _head_level(text: str) -> tuple[int | None, str]:
    """(계층 신호로 읽은 레벨, 기호를 뗀 제목). 신호가 없으면 (None, 원문)."""
    if m := _HEAD_SYMBOL.match(text):
        return _BULLET_DEPTH[m.group(1)] + 2, m.group(2)
    return next((lv for rx, lv in _HEAD_LEVELS if rx.match(text)), None), text


def _relevel_headings(lines: list[str]) -> list[str]:
    heads = [(i, m) for i, ln in enumerate(lines) if (m := _HEAD_RE.match(ln))]
    if len({m.group(1) for _, m in heads}) != 1:
        return lines            # docling이 이미 계층을 구분한 문서 — 그 판단을 존중한다
    leveled = [(i, *_head_level(m.group(2))) for i, m in heads]
    if not any(lv for _, lv, _ in leveled):
        return lines            # 번호도 기호도 없다 — 계층을 지어낼 근거가 없다
    # 무번호 제목은 직전 신호 제목의 한 단계 아래로 둔다. 고정 레벨을 주면 무번호
    # 제목이 기호 제목의 부모인 문서("평가체계" > "¡ 절차")에서 계층이 뒤집힌다.
    # 신호가 아직 없으면(문서 맨 앞) 문서 제목이므로 h1.
    out, signalled, unnumbered = list(lines), 0, None
    for n, (i, level, text) in enumerate(leveled):
        if level is None:
            # 한 섹션 안에서는 무번호 제목의 레벨을 유지한다. 사이에 더 깊은 기호 제목이
            # 끼어도 뒤따르는 무번호 제목은 그 자식이 아니라 앞 무번호 제목의 형제다.
            level = unnumbered or signalled + 1
            if n:
                unnumbered = level      # 맨 앞 제목은 문서 제목이라 기준이 될 수 없다
        else:
            signalled = level
            if level <= (unnumbered or 7):
                unnumbered = None       # 같은 깊이 이상의 신호 제목 = 새 섹션의 시작
        out[i] = "#" * min(level, 6) + " " + text
    return out


# PDF 자간(letter-spacing)으로 한 음절씩 벌어진 텍스트를 되붙인다. 공문서 조판은 강조
# 구간에서 어절 사이를 두 칸, 음절 사이를 한 칸으로 벌리므로 두 칸=어절 경계(→한 칸),
# 한 칸=자간(→제거)로 본다. 음절이 1~2칸 간격으로 4개 이상 이어질 때만 손대므로 정상
# 국문(어절 사이만 한 칸)은 건드리지 않는다. 어절 경계는 1~3칸(강조 조판은 어절을 세 칸
# 까지 벌린다)까지 인정 — 다중 공백은 모두 한 칸으로 접힌다. 다음 음절이 여러 칸이라도
# 정상 어절은 낱 음절이 아니므로 {3,}(4음절 연속) 조건에 걸리지 않아 안전하다.
# ponytail: 자간 경계까지 한 칸뿐이면 어절이 붙는다. 실무 대부분은 두 칸 이상이라 무시.
# (?<![가-힣]): 정상 어절 끝 음절("대한민국 과 학..."의 '국')에서 런이 시작돼 앞 단어를
# 삼키지 않게 한다. 자간 음절은 앞뒤가 공백/비한글로 떨어진 낱 음절이어야 한다.
_SPACED_RUN = re.compile(r"(?<![가-힣])[가-힣](?: {1,3}[가-힣]){3,}")


def _despace(md: str) -> str:
    def collapse(m):  # 두 칸 → 표식, 한 칸 → 삭제, 표식 → 한 칸
        return m.group(0).replace("  ", "\x00").replace(" ", "").replace("\x00", " ")
    return _SPACED_RUN.sub(collapse, md)


# 구두점 주변 과잉 공백 정리(자간과 별개 아티팩트): 가운뎃점("산 · 학 · 연"), 괄호·낫표
# 안쪽 패딩("( 연 )", "｢ 법 ｣"), 연도 약물음표("' 24"→"'24")를 붙인다. 공백만 다루고
# 줄바꿈은 건드리지 않는다. straight quote(' ")의 열림/닫힘은 판별이 모호해 제외한다.
_MIDDOT = re.compile(r" *([·‧⸱・･]) *")   # ･=U+FF65 반각(공문서에서 흔함)
_OPEN = re.compile(r"([｢「『（(\[]) +")
_CLOSE = re.compile(r" +([｣」』）)\]])")
_YEAR = re.compile(r"' +(\d)")


def _tighten(md: str) -> str:
    md = _MIDDOT.sub(r"\1", md)
    md = _OPEN.sub(r"\1", md)
    md = _CLOSE.sub(r"\1", md)
    return _YEAR.sub(r"'\1", md)


def postprocess(md: str) -> str:
    """docling 마크다운 정리. 순서가 규칙의 일부다 — _collapse_spaces는 두 칸을
    어절 경계로 읽는 _despace 뒤, _relevel_headings는 제목이 다 합쳐진 뒤에 온다."""
    lines = _normalize_depths(
        _join_wrapped(_merge_split_headings(_fix_bullets(_PUA_RE.sub("", md)).split("\n"))))
    md = _tighten(_despace("\n".join(lines)))
    # 줄머리 심볼 불릿은 위에서 목록이 됐다. 남은 건 표 셀 안처럼 목록으로 못 바꾸는
    # 자리이므로 눈에 보이는 불릿으로만 바꾼다.
    for sym in _LEFTOVER_SYMBOLS:
        md = md.replace(sym, "•")
    lines = md.split("\n")
    return "\n".join(_relevel_headings(_collapse_spaces(lines)))


def _usable_cpus() -> int:
    """실제로 쓸 수 있는 코어 수. sched_getaffinity는 리눅스 전용이라 폴백을 둔다."""
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 4


def _cuda_available() -> bool:
    """GPU가 실제로 보이는지. 컨테이너에 --gpus를 안 주면 여기서 False가 되고
    아래 설정이 통째로 CPU 값으로 돌아간다 — 이미지·코드는 한 벌만 유지한다.
    이미지의 torch는 이미 CUDA 빌드(2.13+cu130, sm_75~sm_120 포함 → 3060 Ti의
    sm_86 해당)라 GPU용 별도 이미지가 필요 없다."""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:  # torch 미설치(테스트) 또는 드라이버 불일치
        return False


# GPU 배치 크기. 3060 Ti는 VRAM 8GB뿐이고 레이아웃 모델과 TableFormer(ACCURATE)가
# 동시에 상주하므로, CUDA OOM이 나면 여기를 2나 1로 낮춘다(재빌드 불필요).
# 기본 4 = docling 기본값. ponytail: 하드웨어마다 다른 값이라 손잡이로 남긴다.
GPU_BATCH = int(os.environ.get("PDF2MD_GPU_BATCH", "4"))


def _perf_knobs(cuda: bool) -> dict:
    """장치별 배치·큐 상한. 천장이 CPU에선 호스트 RAM, GPU에선 VRAM이라 값이 다르다.

    **큐가 배치를 직접 제한한다.** ThreadedQueue.get_batch는 배치가 찰 때까지 기다리지
    않는다 — 큐에 1개라도 있으면 그때 있는 만큼만 꺼내간다(batch_polling_interval은
    비었을 때의 대기 상한일 뿐이다). 그래서 queue_max_size가 실효 배치의 천장이고,
    CPU 값 queue=2를 그대로 두면 layout_batch_size를 4로 올려도 배치는 영원히 ≤2다.
    실측(51p 공문서, CPU 6코어, 계측 패치로 배치 길이 히스토그램 수집):
        queue=2  batch=1 → layout 평균 배치 1.00 (1×51)
        queue=16 batch=4 → layout 평균 배치 3.64 (4×12, 2×1, 1×1)
    큐를 안 올리면 배치 설정이 死문이 된다는 뜻이다.

    CPU 값(1/1/2)은 16GB 호스트에서 mem_limit 5g를 안 넘기려고 실측으로 깎아둔 것이고,
    CPU에서는 배치를 키워봐야 손해다(같은 51p: 89.5s → 93.7s). torch가 이미 이미지
    한 장에 전 코어를 쓰고 있어 배치로 더 짜낼 여유가 없다.

    GPU에서 값이 뒤집히는 근거 — 같은 문서의 단계별 busy time(CPU, wall 89.5s):
        preprocess  6.1s ( 7%)  ← CPU 전용. 가속 안 됨, 이게 GPU 후의 바닥이 된다
        layout     42.9s (48%)  ← GPU
        table      57.7s (64%)  ← GPU (두 단계는 별도 스레드라 겹쳐서 돈다)
        assemble    0.0s
    무거운 두 단계가 모두 GPU 대상이라 이론상 상한이 크다(preprocess 6.1s 부근까지).
    큐 16은 그 바닥까지 카드를 굶기지 않을 만큼만 깊게 잡은 값이다. docling 기본 100은
    32GB면 감당되지만 in-flight 페이지 수만큼 RAM을 먹고, 실측상 큐를 키워도 속도는
    안 붙었다(100 → 400 변화 없음). GPU에서는 preprocess가 병목이라 큐가 차기보다
    비어 있을 쪽이다 — 더 깊게 잡을 이유가 없다.
    ponytail: 배치 4의 VRAM 실사용은 미측정이다(카드 없는 호스트에서 개발). 8GB가
    빠듯하면 PDF2MD_GPU_BATCH로 낮춘다.
    """
    if cuda:
        return {"queue_max_size": 16,
                "layout_batch_size": GPU_BATCH, "table_batch_size": GPU_BATCH}
    return {"queue_max_size": 2, "layout_batch_size": 1, "table_batch_size": 1}


# 잡마다 컨버터를 새로 만들면 모델을 매번 다시 올린다(실측 CPU 잡당 +0.8s, GPU는
# VRAM 재업로드까지). 컨버터는 원래 재사용하라고 만든 물건이라 캐시가 정답이다.
# maxsize=1인 이유: picture_images 두 값이 각각 모델 한 벌을 더 물어 8GB VRAM에
# 두 벌을 얹기 싫다. 옵션이 번갈아 오면 지금처럼 다시 만들 뿐 더 나빠지지 않는다.
# peak 메모리는 그대로다 — 잡을 도는 동안에는 어차피 모델이 떠 있다.
@lru_cache(maxsize=1)
def _build_converter(*, picture_images: bool = True):
    # 지연 import: 테스트가 torch 없이 돌게 함.
    from docling.datamodel.backend_options import PdfBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # 백엔드는 docling 기본값(DoclingParseDocumentBackend)을 쓴다. 한때 더 가벼운
    # PyPdfiumDocumentBackend로 바꿨었으나, 그 백엔드는 텍스트 셀 추출까지 pdfium의
    # word 단위 분할에 맡겨 한글처럼 글자 bbox가 겹치는 조판에서 어절 끝 음절을 다음
    # 단어에 다시 붙인다("저물고," → "저물고, 고,"). 본문·표·CSV가 모두 오염됐다.
    #
    opts = PdfPipelineOptions()
    opts.do_ocr = False                       # 텍스트 PDF → OCR 모델 미로딩(~2GB 절감)
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.images_scale = 1.25
    # docling 기본값은 False — 켜지 않으면 그림이 통째로 누락된다(rev 3에서 한 번 겪었다).
    # 다만 include_images=false로 부르는 쪽(= /api/convert의 기본값, 에이전트 경로)은
    # save_as_markdown을 PLACEHOLDER로 저장해 만든 그림을 그대로 버린다. 만들고 버리는
    # 일이라 안 만드는 게 맞다. 실측(178p·표 87개): 시간은 그대로 185.4s → 184.7s인데
    # peak 3.31GB → 1.91GB(-42%)다. 마크다운은 md5까지 동일하고, 그림 개수(21)와
    # <!-- image --> 개수(21)도 그대로다 — 그림 '검출'은 레이아웃 모델이 하고, 이 옵션은
    # 검출된 자리를 크롭·인코딩할지만 정한다.
    opts.generate_picture_images = picture_images

    # 메모리: do_ocr=False 다음으로 큰 레버가 파이프라인에 동시에 떠 있는 페이지 수다.
    # 여기 한때 settings.perf.page_batch_size=1이 있었는데, 그건 PaginatedPipeline의
    # knob이라 StandardPdfPipeline(스테이지+큐 구조)에서는 읽히지도 않는 죽은 줄이었다
    # — MRO에 PaginatedPipeline이 없다. 실제 상한은 queue_max_size(기본 100)다.
    # 실측: 178p·표 87개 3.90GB → 3.30GB(-15%), 51p 2.78GB → 2.20GB(-19%).
    # 속도와 출력은 그대로다(178p 190.6s → 192.0s, 마크다운 문자수·표 개수 동일).
    # 큐를 키우는 쪽은 의미가 없다 — 400으로 4배 늘려도 67.1s로 기본값과 같고 메모리만
    # 늘었다. 병목이 CPU 추론이라 버퍼를 넓혀도 더 태울 여유가 없다.
    # ponytail: 이미지 객체가 수십만 개인 병리적 페이지(실측: 27p 문서의 한 페이지에
    # 609,831개)는 백엔드의 비트맵 파싱만으로 4GB를 써 이걸로도 못 막는다. 그런 문서는
    # worker가 재시도 없이 실패시킨다(_MAX_ATTEMPTS=1) — 실측 peak 6.1GB.
    cuda = _cuda_available()
    knobs = _perf_knobs(cuda)
    opts.queue_max_size = knobs["queue_max_size"]
    opts.layout_batch_size = knobs["layout_batch_size"]
    opts.table_batch_size = knobs["table_batch_size"]
    # device는 docling 기본값 'auto'로 둔다 — GPU가 보이면 cuda:0, 아니면 cpu다.
    # 명시적으로 'cuda'를 박으면 GPU가 안 보일 때 조용히 CPU로 떨어지는 대신 뭐가
    # 잘못됐는지 알기 어려워진다. 대신 어느 쪽으로 붙었는지는 로그로 남긴다 —
    # 이 줄이 cpu면 --gpus나 nvidia-container-toolkit이 빠진 것이다.
    print(f"[pdf2md] device={'cuda' if cuda else 'cpu'} "
          f"layout_batch={opts.layout_batch_size} table_batch={opts.table_batch_size} "
          f"queue={opts.queue_max_size} threads={_usable_cpus()}", flush=True)

    # 속도: docling 기본 num_threads는 4로 고정인데 torch 자체 기본값은 코어 수다 —
    # 6코어 호스트에서 docling이 오히려 낮춰 잡고 있었다. 실측(코어 6, 51p, 2회 평균):
    # 1스레드 139.6s / 2 84.4s / 4 65.6s / 6 57.1s / 8 67.6s — 코어 수에서 최적이고
    # 넘기면(8) 오버서브스크립션으로 4보다도 느리다. 178p도 190.6s → 178.0s(-6.6%).
    # 메모리·출력은 무관하다(전 구간 peak 2.1~2.2GB, 마크다운 문자수 동일).
    # 하드코딩하지 않는 이유: 이 서비스는 집 Proxmox와 Oracle VPS 양쪽에서 도는데
    # 코어 수가 다르면 고정값이 그대로 오버서브스크립션이 된다.
    # os.cpu_count()가 아니라 sched_getaffinity인 이유: 컨테이너 안에서 cpu_count()는
    # LXC의 cpuset을 무시하고 물리 호스트를 본다(실측: 컨테이너에서 16, 실제 가용 6).
    # 그대로 쓰면 16스레드가 되어 기본값 4보다도 느려진다.
    # ponytail: docker의 --cpus(cgroup 쿼터)까지는 안 본다 — affinity는 그대로라
    # 못 잡는다. 지금 compose에 cpus 제한이 없어 문제되지 않는다. 걸게 되면
    # /sys/fs/cgroup/cpu.max의 쿼터도 함께 봐야 한다.
    # GPU에서도 이 값은 그대로 의미가 있다. 추론만 카드로 넘어가고 페이지 비트맵
    # 파싱·후처리는 여전히 CPU라, 그쪽이 느리면 GPU가 굶는다.
    opts.accelerator_options.num_threads = _usable_cpus()

    # enforce_same_font=False: 기본값(True)은 폰트가 바뀌는 자리에서 텍스트 셀을 쪼갠다.
    # 공문서는 낫표·괄호를 본문과 다른 폰트로 찍는 일이 흔해, "｢국가전략기술 선정(안)｣을
    # 별지와 같이"가 본문과 "｢ ( ) ｣" 두 줄로 갈렸다. 실측: 커버리지 72.0%→78.0%,
    # 60.7%→61.7%, 표 개수·음절 중복 변화 없음.
    backend_options = PdfBackendOptions(enforce_same_font=False)
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(
            pipeline_options=opts, backend_options=backend_options)}
    )


def convert(pdf_path, out_dir, *, include_images: bool, include_tables_csv: bool):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "doc.md"

    result = _build_converter(picture_images=include_images).convert(str(pdf_path))
    doc = result.document

    try:
        # docling_core is a light, torch-free dependency of docling itself;
        # optional here so unit tests (fake converter, no docling installed) still run.
        from docling_core.types.doc import ImageRefMode
        image_mode = ImageRefMode.REFERENCED if include_images else ImageRefMode.PLACEHOLDER
    except ImportError:
        image_mode = "referenced" if include_images else "placeholder"
    # artifacts_dir="images"로 직접 지정 → doc.md에 상대경로(images/...)가 그대로 기록됨
    # (폴더 rename + 텍스트 치환은 절대경로가 남는 버그가 있어 제거).
    doc.save_as_markdown(str(md_path), artifacts_dir=Path("images"), image_mode=image_mode)
    # docling이 본문을 HTML 이스케이프한 채 마크다운에 내보낸다("R&amp;D"). 되돌린다.
    md = html.unescape(md_path.read_text(encoding="utf-8"))
    md_path.write_text(postprocess(md), encoding="utf-8")

    n_tables = len(getattr(doc, "tables", None) or [])
    tables_dir = out_dir / "tables"
    if include_tables_csv and n_tables:
        tables_dir.mkdir(exist_ok=True)
        for i, table in enumerate(doc.tables, 1):
            df = table.export_to_dataframe(doc=doc)
            # utf-8-sig(BOM): Excel은 BOM이 없으면 CSV를 시스템 인코딩(한국어 Windows는
            # CP949)으로 읽어 한글이 깨진다. BOM 3바이트가 UTF-8임을 알려준다.
            df.to_csv(tables_dir / f"table-{i:02d}.csv", index=False, encoding="utf-8-sig")

    # n_images: 문서의 실제 그림 개수(옵션과 무관하게 정확) — n_tables와 대칭.
    n_images = len(getattr(doc, "pictures", None) or [])

    # ZIP 패키징
    zip_path = out_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(md_path, "doc.md")
        for sub in ("images", "tables"):
            d = out_dir / sub
            if d.exists():
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        z.write(f, str(f.relative_to(out_dir)))

    return n_tables, n_images

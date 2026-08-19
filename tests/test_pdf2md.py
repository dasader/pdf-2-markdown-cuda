import os
import inspect
import time
from bisect import bisect_left

import pytest
from app import config, db


def _use_tmp_data(monkeypatch, tmp_path):
    """config의 데이터 경로를 tmp_path로 돌린다. 스토리지 생성은 호출자가 결정."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    _use_tmp_data(monkeypatch, tmp_path)
    config.ensure_dirs()
    db.init_db()
    c = db.connect()
    yield c
    c.close()


def _job(conn, jid, session="s1", sha="abc", opts="o1", status="queued", result_dir=None):
    db.create_job(conn, id=jid, session_id=session, filename="f.pdf", sha256=sha,
                  opts_hash=opts, status=status, page_total=3, result_dir=result_dir)


def test_create_and_get_job(conn):
    _job(conn, "j1")
    row = db.get_job(conn, "j1")
    assert row["status"] == "queued"
    assert row["filename"] == "f.pdf"


def test_list_jobs_session_isolation(conn):
    _job(conn, "j1", session="s1")
    _job(conn, "j2", session="s2")
    mine = db.list_jobs(conn, "s1", admin=False)
    assert [r["id"] for r in mine] == ["j1"]
    all_jobs = db.list_jobs(conn, "s1", admin=True)
    assert {r["id"] for r in all_jobs} == {"j1", "j2"}


def test_find_cached_returns_done_only(conn):
    _job(conn, "j1", sha="X", opts="O", status="queued")
    assert db.find_cached(conn, "X", "O") is None
    _job(conn, "j2", sha="X", opts="O", status="done", result_dir="/r/X-O")
    hit = db.find_cached(conn, "X", "O")
    assert hit["result_dir"] == "/r/X-O"


def test_claim_next_queued_atomic(conn):
    _job(conn, "j1", status="queued")
    claimed = db.claim_next_queued(conn)
    assert claimed["id"] == "j1"
    assert db.get_job(conn, "j1")["status"] == "running"
    assert db.claim_next_queued(conn) is None  # 더 없음


def test_count_queued(conn):
    _job(conn, "j1", session="s1", status="queued")
    _job(conn, "j2", session="s1", status="done")
    assert db.count_queued(conn, "s1") == 1


def test_finish_job_sets_n_tables_n_images(conn):
    _job(conn, "j1", status="running")
    db.finish_job(conn, "j1", status="done", n_tables=3, n_images=5)
    row = db.get_job(conn, "j1")
    assert row["n_tables"] == 3
    assert row["n_images"] == 5
    # calling again without n_tables/n_images leaves them unchanged
    db.finish_job(conn, "j1", status="done")
    row = db.get_job(conn, "j1")
    assert row["n_tables"] == 3
    assert row["n_images"] == 5


def test_active_created_ats_lists_queued_and_running_only(conn):
    db.create_job(conn, id="j1", session_id="s1", filename="f.pdf", sha256="a",
                   opts_hash="o", status="queued", page_total=1, result_dir=None)
    conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (100.0, "j1"))
    db.create_job(conn, id="j2", session_id="s1", filename="f.pdf", sha256="a",
                  opts_hash="o", status="running", page_total=1, result_dir=None)
    conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (200.0, "j2"))
    db.create_job(conn, id="j3", session_id="s1", filename="f.pdf", sha256="a",
                  opts_hash="o", status="done", page_total=1, result_dir=None)
    conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (300.0, "j3"))
    conn.commit()
    # j1(queued) and j2(running) are active in created_at order; j3 is done -> excluded
    actives = db.active_created_ats(conn)
    assert actives == [100.0, 200.0]
    # bisect_left == "strictly before" count, matching the ahead semantics in _serialize
    assert bisect_left(actives, 250.0) == 2
    assert bisect_left(actives, 150.0) == 1
    assert bisect_left(actives, 100.0) == 0


def test_worker_busy(conn):
    _job(conn, "j1", status="queued")
    assert db.worker_busy(conn) is False
    db.claim_next_queued(conn)
    assert db.worker_busy(conn) is True


def test_requeue_running_resets_orphans(conn):
    _job(conn, "j1", status="queued")
    db.claim_next_queued(conn)
    assert db.get_job(conn, "j1")["status"] == "running"
    assert db.requeue_running(conn) == 1
    row = db.get_job(conn, "j1")
    assert row["status"] == "queued"
    assert row["started_at"] is None


from pathlib import Path
from app import convert

FIX = Path(__file__).parent / "fixtures" / "sample.pdf"


def test_is_pdf_magic_bytes():
    assert convert.is_pdf(b"%PDF-1.7 ...")
    assert not convert.is_pdf(b"PK\x03\x04zip")


def test_probe(_=None):
    pages, chars = convert.probe(FIX)
    assert pages == 1
    assert chars > config.MIN_TEXT_CHARS   # 텍스트 레이어가 있는 PDF


def test_opts_hash_stable_and_distinct():
    a = convert.opts_hash(True, True)
    assert a == convert.opts_hash(True, True)
    assert a != convert.opts_hash(False, True)
    assert a != convert.opts_hash(True, False)


def test_opts_hash_changes_with_converter_rev(monkeypatch):
    # CONVERTER_REV를 올리면 opts_hash가 달라져 find_cached가 옛 결과를 못 찾는다.
    # 변환 로직이 바뀌었을 때 캐시가 스스로 무효화되는 유일한 장치.
    before = convert.opts_hash(True, True)
    monkeypatch.setattr(convert, "CONVERTER_REV", convert.CONVERTER_REV + 1)
    assert convert.opts_hash(True, True) != before


def test_fix_bullets():
    md = "\n".join([
        "- □ (배경) 기술패권 경쟁",
        "- ㅇ 주요국은 전략기술 분야를 선정",
        "- ㅇ",                                    # 기호만, 본문은 다음 줄
        "- 별지와 같이 상정함",
        "- ◇ 정합성 분석",                         # ◇ → 0단계
        "- ▷ 세부 검토",                           # ▷ → 1단계
        "- ① (기술발굴) 패키지 통합 필요",          # 순번 기호 — 지우면 뜻이 사라진다
        "- → 관련 정책 추진중",                    # 불릿 기호가 아님 — 건드리지 않는다
        "| <1> 반도체 | ▪ 고용량 메모리 |",        # 표 행 — 건드리지 않는다
        "## 제목",
    ])
    out = convert._fix_bullets(md).split("\n")
    assert out[0] == "- (배경) 기술패권 경쟁"        # □ → 0단계
    assert out[1] == "  - 주요국은 전략기술 분야를 선정"  # ㅇ → 1단계
    # 빈 불릿 줄은 사라지고, 그 본문인 다음 줄이 기호의 자리(ㅇ = 1단계)를 물려받는다.
    assert out[2] == "  - 별지와 같이 상정함"
    assert out[3] == "- 정합성 분석"
    assert out[4] == "  - 세부 검토"
    assert out[5] == "- ① (기술발굴) 패키지 통합 필요"
    assert out[6] == "- → 관련 정책 추진중"
    assert out[7] == "| <1> 반도체 | ▪ 고용량 메모리 |"
    assert out[8] == "## 제목"


def test_fix_bullets_unsymboled_item_becomes_child():
    # docling이 원문 하위 불릿('-')을 마크다운 불릿으로 흡수해 기호를 지운다.
    # 들여쓰기만 믿으면 부모(¡)와 자식이 뒤집힌다.
    md = "\n".join([
        "## 위원회 구성",
        "- ¡ (위원회 체계) 위원장 1인을 포함한 5인 이상",
        "- R&D 자체평가위원회는 총괄위원회와 분과위원회로 구성",
        "- ¡ (총괄위원회 구성) 산학연 전문가를 포함",
        "본문 문단",                              # 목록 블록의 끝
        "- 기호 없이 새로 시작하는 항목",
    ])
    assert convert._fix_bullets(md).split("\n") == [
        "## 위원회 구성",
        "  - (위원회 체계) 위원장 1인을 포함한 5인 이상",
        "    - R&D 자체평가위원회는 총괄위원회와 분과위원회로 구성",
        "  - (총괄위원회 구성) 산학연 전문가를 포함",
        "본문 문단",
        "- 기호 없이 새로 시작하는 항목",
    ]


def test_fix_bullets_drops_bare_symbol_lines():
    # 내용 없이 기호만 남은 줄(체크리스트 서식에서 흔함)
    assert convert._fix_bullets("본문\n¡\n- 항목") == "본문\n- 항목"


def test_fix_bullets_repeated_symbol():
    # 같은 기호를 두 번 찍는 조판(페이지 머리말)
    assert convert._fix_bullets("- ▪ ▪   2025년도 자체평가 지침") == "    - 2025년도 자체평가 지침"
    # 본문이 기호와 같은 글자로 시작하면 그건 반복이 아니다 — 먹지 않는다.
    assert convert._fix_bullets("- ㅇ ㅇㅇ은행 관련") == "  - ㅇㅇ은행 관련"
    # 기호 뒤에 공백이 없는 조판도 있다.
    assert convert._fix_bullets("- ▪사업추진(집행) 유형") == "    - 사업추진(집행) 유형"


def test_fix_bullets_soft_hyphen():
    # 공문서 하위 불릿 '-'가 soft hyphen(U+00AD)으로 조판돼 안 보이는 글자로 남는다.
    assert convert._fix_bullets("- ­ 최근 수행되는 특정평가") == "    - 최근 수행되는 특정평가"


def test_postprocess_symbol_bullet_in_table():
    # 심볼폰트 불릿이 'Ÿ'·'¡'로 추출된다. 표 셀 안이라 목록으로는 못 바꾸고 기호만 바꾼다.
    out = convert.postprocess("| 평가이슈 | Ÿ 다부처사업 추진의 적절성 ¡ (과정) 증빙 미제출 |")
    assert "• 다부처사업 추진의 적절성 • (과정) 증빙 미제출" in out
    # 줄머리에 오면 기호가 아니라 목록이 된다.
    assert convert.postprocess("- Ÿ 단일 추진형") == "- 단일 추진형"


def test_postprocess_strips_pua_glyphs():
    # 심볼폰트 글리프가 매핑 없이 사유영역(PUA) 코드포인트로 나온 것 — 뜻이 없다.
    assert convert.postprocess("##### \uf000 자체평가결과") == "##### 자체평가결과"


def test_fix_bullets_notes_are_not_list_items():
    md = "\n".join([
        "- ※ 패스트트랙을 계속사업으로 전환",       # 비고 — 불릿 해제
        "* 주요국 창업기업 생존율(5년) : 33.8%",    # 각주 — 마크다운 불릿으로 렌더되면 안 됨
        "- ** 韓 유니콘기업 진입 수 : 2개",
        "- **강조** 가 들어간 정상 항목",           # 인라인 서식 — 각주가 아니다
    ])
    out = [ln for ln in convert._fix_bullets(md).split("\n") if ln]
    assert out[0] == "※ 패스트트랙을 계속사업으로 전환"
    assert out[1] == r"\* 주요국 창업기업 생존율(5년) : 33.8%"
    assert out[2] == r"\*\* 韓 유니콘기업 진입 수 : 2개"
    assert out[3] == "- **강조** 가 들어간 정상 항목"
    # 각주 앞에는 빈 줄이 붙는다 — 없으면 마크다운이 앞 항목에 이어붙인다.
    assert convert._fix_bullets("- 본문\n* 각주") == "- 본문\n\n\\* 각주"


def test_merge_split_headings():
    lines = ["## 2", "", "## 평가방향", "## 3 기술사업화 대상 사업", "본문"]
    assert convert._merge_split_headings(lines) == [
        "## 2 평가방향", "## 3 기술사업화 대상 사업", "본문"]
    # 뒤에 제목이 없으면 그대로 둔다(번호가 본문일 수도 있다).
    assert convert._merge_split_headings(["## 2", "본문"]) == ["## 2", "본문"]


def test_join_wrapped_bullets():
    lines = [
        "- 기술사업화 실적을 평가하여 예산과정에 환류하고, 사업전략 수정,",
        "- 추진체계 개편 등을 통해 사업목표를 달성",
        "- 국가R&D 성과가 특허 등에 그치지 않고, 기술이전 및",
        "- 기술사업화로 경제적 가치를 창출",
        "- 정상적으로 끝난 항목임",
        "- 다음 항목은 이어붙이지 않는다",
        "  - 들여쓰기가 달라도 미완이면,",   # docling은 잘린 뒷줄을 최상위로 내보낸다
        "- 이어붙인다",
    ]
    assert convert._join_wrapped(lines) == [
        "- 기술사업화 실적을 평가하여 예산과정에 환류하고, 사업전략 수정, 추진체계 개편 등을 통해 사업목표를 달성",
        "- 국가R&D 성과가 특허 등에 그치지 않고, 기술이전 및 기술사업화로 경제적 가치를 창출",
        "- 정상적으로 끝난 항목임",
        "- 다음 항목은 이어붙이지 않는다",
        "  - 들여쓰기가 달라도 미완이면, 이어붙인다",
    ]


def test_join_wrapped_bullets_by_continuation_marker():
    # 뒷줄이 어미·조사로 시작하면 앞줄이 끝나지 않은 것 — 한국어 문장은 그렇게 시작 못 한다.
    lines = ["- 특정평가의 절차와 체계를 정비하고, 이를 운영지침 등으로 명문화",
             "- 하여 특정평가에 대한 부처의 이해도 향상",
             "- 수출 실적, 해외 파트너링, 글로벌 인재채용",
             "- 등 지표가 국제화 중심으로 설계됨",
             "- 등록 절차는 별도 항목이다"]     # '등록'은 '등'이 아니다
    assert convert._join_wrapped(lines) == [
        "- 특정평가의 절차와 체계를 정비하고, 이를 운영지침 등으로 명문화 하여 특정평가에 대한 부처의 이해도 향상",
        "- 수출 실적, 해외 파트너링, 글로벌 인재채용 등 지표가 국제화 중심으로 설계됨",
        "- 등록 절차는 별도 항목이다"]


def test_normalize_depths():
    # 부모 없는 4칸 들여쓰기는 마크다운에서 코드 블록이 된다 — 상대 깊이만 남긴다.
    lines = ["#### 제목", "    - 첫 항목", "    - 둘째 항목", "        - 하위", "  - 되돌아옴",
             "본문", "    - 새 블록의 첫 항목"]
    assert convert._normalize_depths(lines) == [
        "#### 제목", "- 첫 항목", "- 둘째 항목", "  - 하위", "- 되돌아옴",
        "본문", "- 새 블록의 첫 항목"]


def test_collapse_spaces():
    lines = [
        "## 1-3.  체계성  -  부처  간  연계  강화",
        "  - 들여쓰기는  보존",
        "| 연번   | 부처명    |",              # 표 행 — 셀 정렬 유지
    ]
    assert convert._collapse_spaces(lines) == [
        "## 1-3. 체계성 - 부처 간 연계 강화",
        "  - 들여쓰기는 보존",
        "| 연번   | 부처명    |",
    ]


def test_relevel_headings():
    lines = ["## Ⅰ 배경 및 방향", "## 1 평가배경 및 필요성", "## 1-3. 체계성",
             "## 가. 세부항목", "## 부처 간 유사･중복 해소", "본문"]
    assert convert._relevel_headings(lines) == [
        "# Ⅰ 배경 및 방향", "## 1 평가배경 및 필요성", "### 1-3. 체계성",
        "### 가. 세부항목", "#### 부처 간 유사･중복 해소", "본문"]


def test_relevel_headings_by_bullet_symbol():
    # 번호 대신 불릿 기호로 계층을 표시하는 공문서. 기호는 떼고 깊이만 남긴다.
    lines = ["## R&D사업 특정평가 개선 방향",     # 맨 앞 무번호 제목 = 문서 제목
             "## ■ 특정평가 목적", "## ㅇ더불어 민주당", "## (실천 01) R&D 투자시스템 혁신"]
    assert convert._relevel_headings(lines) == [
        "# R&D사업 특정평가 개선 방향",
        "## 특정평가 목적", "### 더불어 민주당", "#### (실천 01) R&D 투자시스템 혁신"]


def test_relevel_headings_unnumbered_can_be_parent():
    # 무번호 제목이 기호 제목의 부모인 문서. 무번호를 최하위로 고정하면 계층이 뒤집힌다.
    lines = ["## Ⅱ. 제도 개요", "## 평가체계", "## ¡ 절차", "## ¡ 대상사업",
             "## 평가결과 산출"]
    # 그리고 '평가결과 산출'은 '¡ 절차'의 자식이 아니라 '평가체계'의 형제다.
    assert convert._relevel_headings(lines) == [
        "# Ⅱ. 제도 개요", "## 평가체계", "### 절차", "### 대상사업", "## 평가결과 산출"]


def test_relevel_headings_respects_existing_hierarchy():
    lines = ["# 큰제목", "## 1 작은제목"]     # docling이 이미 계층을 구분한 문서
    assert convert._relevel_headings(lines) == lines


def test_relevel_headings_skips_documents_without_signals():
    # 번호도 기호도 없으면 계층을 지어낼 근거가 없다 — docling 출력을 그대로 둔다.
    lines = ["## 발표자료 제목", "## 배경", "## 결론"]
    assert convert._relevel_headings(lines) == lines


def test_fix_bullets_symbol_l():
    md = "\n".join([
        "- l (구성) 위원장 및 부위원장",   # Wingdings 'l'(▪) → 심볼 제거
        "- l",                            # 기호만 → 사라짐
        "- long-term 전략은 유지",         # 실제 'l' 낱말 — 건드리지 않는다
    ])
    out = convert._fix_bullets(md).split("\n")
    assert out[0] == "- (구성) 위원장 및 부위원장"
    assert out[1] == "- long-term 전략은 유지"


def test_despace():
    # 실제 변환 출력에서 가져온 자간 깨짐 사례. 두 칸=어절 경계, 한 칸=자간.
    assert convert._despace("글 로 벌  기 술 패 권  경 쟁") == "글로벌 기술패권 경쟁"
    assert convert._despace("고 도 화  및  제 도 혁 신") == "고도화 및 제도혁신"
    # 정상 국문(어절 사이 한 칸, 음절은 붙음)은 건드리지 않는다.
    assert convert._despace("국가 전략 기술 개발") == "국가 전략 기술 개발"
    # 음절 3개 이하(임계 미만)는 오검을 피해 그대로 둔다.
    assert convert._despace("심 화") == "심 화"
    # 앞의 정상 어절(대한민국)을 자간 런에 흡수하지 않는다.
    assert convert._despace("대한민국 과 학 기 술 주 권") == "대한민국 과학기술주권"
    # 강조 조판의 세 칸 어절 경계도 접는다.
    assert convert._despace("통 합   자 율   비 행 체") == "통합 자율 비행체"
    # 세 칸으로 벌어진 정상(다음절) 어절은 건드리지 않는다.
    assert convert._despace("국가   전략   기술   개발") == "국가   전략   기술   개발"


def test_tighten():
    assert convert._tighten("산 · 학 · 연") == "산·학·연"
    assert convert._tighten("육성 · 확보") == "육성·확보"          # 어절 사이 가운뎃점도 붙임
    assert convert._tighten("유사 ･ 중복") == "유사･중복"          # 반각 가운뎃점(U+FF65)
    assert convert._tighten("제주 ( 그린수소 )") == "제주 (그린수소)"  # 괄호 안쪽만
    assert convert._tighten("｢ 국가전략기술육성법 ｣") == "｢국가전략기술육성법｣"
    assert convert._tighten("( ' 24~ ' 28)") == "('24~ '28)"  # 연도는 붙임(틸드 뒤 한 칸은 잔류)
    assert convert._tighten("| 「법」, 과기정통부 |") == "| 「법」, 과기정통부 |"  # 정상 표 행 불변


def test_build_converter_pipeline_options():
    # docling 기본값은 generate_picture_images=False다. 이 줄이 지워지면 그림이 통째로
    # 누락되는데, n_images는 크롭이 아니라 인식된 그림 영역 수를 세므로 눈치채기 어렵다.
    pytest.importorskip("docling")
    from docling.datamodel.base_models import InputFormat

    conv = convert._build_converter()
    fmt = conv.format_to_options[InputFormat.PDF]
    assert fmt.pipeline_options.generate_picture_images is True
    assert fmt.pipeline_options.do_table_structure is True
    assert fmt.pipeline_options.do_ocr is False
    # True(기본)면 낫표·괄호가 다른 폰트라는 이유로 본문에서 떨어져 나온다.
    assert fmt.backend_options.enforce_same_font is False
    # 동시에 떠 있는 페이지 수 제한(178p 문서 peak 3.90GB → 3.30GB). 기본값 100/4로
    # 돌아가면 5GB 워커의 OOM 여유가 1.7GB에서 1.1GB로 줄어든다.
    assert fmt.pipeline_options.queue_max_size == 2
    assert fmt.pipeline_options.layout_batch_size == 1
    assert fmt.pipeline_options.table_batch_size == 1
    # docling 기본값 4는 6코어 호스트에서 13% 손해였다. 가용 코어 수를 따라가야 한다.
    assert fmt.pipeline_options.accelerator_options.num_threads == convert._usable_cpus()


def test_build_converter_skips_picture_crop_when_images_off():
    """include_images=false면 그림을 만들지 않는다.

    그 경로는 save_as_markdown을 PLACEHOLDER로 저장해 만든 그림을 그대로 버린다.
    실측(178p): 시간 185.4s → 184.7s로 그대로인데 peak 3.31GB → 1.91GB(-42%).
    마크다운은 md5까지 동일하다. 이 인자가 지워져 항상 True로 돌아가면 API 호출마다
    쓰지도 않을 그림 크롭에 1.4GB를 다시 쓰게 된다.
    """
    pytest.importorskip("docling")
    from docling.datamodel.base_models import InputFormat

    off = convert._build_converter(picture_images=False)
    assert off.format_to_options[InputFormat.PDF].pipeline_options.generate_picture_images is False


def test_convert_passes_include_images_to_pipeline(tmp_path, monkeypatch):
    """convert()가 include_images를 파이프라인까지 넘기는지. 여기가 끊기면 위 테스트가
    통과해도 실제 호출은 여전히 그림을 만든다."""
    seen = []

    class FakeDoc:
        tables = []
        pictures = []

        def save_as_markdown(self, path, **kw):
            Path(path).write_text("# x", encoding="utf-8")

    class FakeResult:
        document = FakeDoc()

    class FakeConverter:
        def convert(self, _):
            return FakeResult()

    def fake_build(*, picture_images=True):
        seen.append(picture_images)
        return FakeConverter()

    monkeypatch.setattr(convert, "_build_converter", fake_build)
    convert.convert(FIX, tmp_path / "out",
                    include_images=False, include_tables_csv=False)
    convert.convert(FIX, tmp_path / "out2",
                    include_images=True, include_tables_csv=False)
    assert seen == [False, True]


def test_usable_cpus_respects_cpuset():
    """컨테이너 안에서 os.cpu_count()는 LXC의 cpuset을 무시하고 물리 호스트를 본다
    (실측: 컨테이너에서 16, 실제 가용 6). 그 값을 쓰면 스레드를 과다 배정한다."""
    n = convert._usable_cpus()
    assert n >= 1
    if hasattr(os, "sched_getaffinity"):
        assert n == len(os.sched_getaffinity(0))


def test_pipeline_is_not_paginated():
    """옛 settings.perf.page_batch_size가 죽은 knob이었던 이유를 못박아 둔다.

    그 값은 PaginatedPipeline만 읽는데 실제 쓰이는 파이프라인은 그걸 상속하지 않는다.
    docling이 다시 PaginatedPipeline 계열로 돌아가면 이 테스트가 깨지고, 그때는
    queue_max_size 대신 page_batch_size를 봐야 한다는 신호가 된다.
    """
    pytest.importorskip("docling")
    from docling.datamodel.base_models import InputFormat
    from docling.pipeline.base_pipeline import PaginatedPipeline

    cls = convert._build_converter().format_to_options[InputFormat.PDF].pipeline_cls
    assert not issubclass(cls, PaginatedPipeline)
    assert "queue_max_size" in inspect.getsource(cls)


def test_convert_packages_zip(tmp_path, monkeypatch):
    # docling을 가짜로 대체: doc.md만 쓰고 tables/pictures 없음.
    class FakeDoc:
        tables = []
        pictures = []
        def save_as_markdown(self, path, artifacts_dir=None, image_mode=None):
            Path(path).write_text("# hi\n")
    class FakeResult:
        document = FakeDoc()
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda **kw: FakeConverter())
    out = tmp_path / "X-O"
    result = convert.convert(FIX, out, include_images=True, include_tables_csv=True)
    assert (out / "doc.md").exists()
    assert (out / "result.zip").exists()
    import zipfile
    names = zipfile.ZipFile(out / "result.zip").namelist()
    assert "doc.md" in names
    assert result == (0, 0)


def test_convert_counts_n_images_regardless_of_include_images(tmp_path, monkeypatch):
    # n_images는 include_images=False여도 문서의 실제 그림 개수를 반영해야 함(n_tables와 대칭).
    class FakeDoc:
        tables = []
        pictures = [object(), object()]
        def save_as_markdown(self, path, artifacts_dir=None, image_mode=None):
            Path(path).write_text("# hi\n")
    class FakeResult:
        document = FakeDoc()
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda **kw: FakeConverter())
    out = tmp_path / "Z-O"
    n_tables, n_images = convert.convert(
        FIX, out, include_images=False, include_tables_csv=False)
    assert n_images == 2
    assert n_tables == 0
    # include_images=False -> no images/ dir written
    assert not (out / "images").exists()


def test_convert_writes_table_csv_and_counts_n_tables(tmp_path, monkeypatch):
    import pandas as pd

    class FakeTable:
        def export_to_dataframe(self, doc=None):
            return pd.DataFrame({"기술분야": ["반도체", "양자"], "b": [3, 4]})

    class FakeDoc:
        tables = [FakeTable()]
        pictures = []
        def save_as_markdown(self, path, artifacts_dir=None, image_mode=None):
            Path(path).write_text("# hi\n")
    class FakeResult:
        document = FakeDoc()
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda **kw: FakeConverter())
    out = tmp_path / "Y-O"
    n_tables, n_images = convert.convert(
        FIX, out, include_images=False, include_tables_csv=True)
    assert n_tables == 1
    assert n_images == 0
    csv = out / "tables" / "table-01.csv"
    assert csv.exists()
    # BOM이 없으면 Excel이 시스템 인코딩(한국어 Windows는 CP949)으로 읽어 한글이 깨진다.
    assert csv.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "반도체" in csv.read_text(encoding="utf-8-sig")
    import zipfile
    names = zipfile.ZipFile(out / "result.zip").namelist()
    assert "tables/table-01.csv" in names


def test_convert_image_refs_are_relative_real_docling_core(tmp_path, monkeypatch):
    # 회귀 테스트: 실제 docling_core DoclingDocument로 이미지 1개를 만들어
    # save_as_markdown(REFERENCED)의 참조 경로가 절대경로가 아니라
    # "images/..." 상대경로인지 확인한다 (Finding 1의 재발 방지).
    docling_core = pytest.importorskip("docling_core")
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    from docling_core.types.doc import DoclingDocument

    real_doc = DoclingDocument(name="test")
    img = Image.new("RGB", (4, 4), color="red")
    from docling_core.types.doc.document import ImageRef
    real_doc.add_picture(image=ImageRef.from_pil(img, dpi=72))

    class FakeResult:
        document = real_doc
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda **kw: FakeConverter())
    out = tmp_path / "W-O"
    n_tables, n_images = convert.convert(
        FIX, out, include_images=True, include_tables_csv=False)

    md = (out / "doc.md").read_text(encoding="utf-8")
    assert "images/" in md
    assert "/tmp" not in md
    assert str(out) not in md  # no absolute path leaked into the markdown
    assert not md.count("](/")  # no reference starts with a leading slash
    assert (out / "images").is_dir()
    assert any((out / "images").iterdir())
    assert n_images == 1


from app import worker


def test_process_one_success(conn, tmp_path, monkeypatch):
    _job(conn, "j1", sha="Y", opts="O", status="queued")
    # 업로드 파일 존재해야 함
    (config.UPLOADS_DIR / "Y.pdf").write_bytes(b"%PDF-1.7")
    called = {}
    def fake_convert(pdf, out, **kw):
        called["out"] = str(out); Path(out).mkdir(parents=True, exist_ok=True)
        return (3, 1)
    monkeypatch.setattr(worker.convert, "convert", fake_convert)
    monkeypatch.setattr(worker.convert, "opts_hash", lambda *a: "O")

    assert worker.process_one(conn) is True
    row = db.get_job(conn, "j1")
    assert row["status"] == "done"
    assert row["result_dir"] == called["out"]
    assert row["n_tables"] == 3
    assert row["n_images"] == 1


def test_process_one_fails_job_with_undecodable_opts_hash(conn, monkeypatch):
    # CONVERTER_REV가 오른 뒤 남아있던 옛 queued 잡: opts_hash를 4조합 어디에도 되짚을
    # 수 없다. 조용히 기본 옵션으로 변환하지 말고 실패시켜야 한다.
    _job(conn, "j1", sha="Y", opts="STALE-REV-1", status="queued")
    (config.UPLOADS_DIR / "Y.pdf").write_bytes(b"%PDF-1.7")
    def never(*a, **k): raise AssertionError("옛 잡을 변환하면 안 된다")
    monkeypatch.setattr(worker.convert, "convert", never)

    assert worker.process_one(conn) is True
    row = db.get_job(conn, "j1")
    assert row["status"] == "failed"
    assert "다시 업로드" in row["error"]


def test_process_one_failure_records_error(conn, monkeypatch):
    _job(conn, "j1", sha="Z", opts="O", status="queued")
    (config.UPLOADS_DIR / "Z.pdf").write_bytes(b"%PDF-1.7")
    def boom(*a, **k): raise RuntimeError("docling exploded")
    monkeypatch.setattr(worker.convert, "convert", boom)
    monkeypatch.setattr(worker.convert, "opts_hash", lambda *a: "O")

    assert worker.process_one(conn) is True
    row = db.get_job(conn, "j1")
    assert row["status"] == "failed"
    assert "docling exploded" in row["error"]


def test_process_one_empty(conn):
    assert worker.process_one(conn) is False


def test_requeue_running_increments_attempts(conn):
    _job(conn, "j1", status="queued")
    db.claim_next_queued(conn)
    assert db.requeue_running(conn) == 1
    assert db.get_job(conn, "j1")["attempts"] == 1
    db.claim_next_queued(conn)
    db.requeue_running(conn)
    assert db.get_job(conn, "j1")["attempts"] == 2


def test_process_one_fails_after_max_attempts(conn, monkeypatch):
    # 워커를 한 번 죽인(OOM) 문서는 재시도하지 않고 바로 실패로 마감한다. 저사양
    # 재시도는 메모리를 못 줄여(실측 6.1GB→6.2GB) 워커만 한 번 더 죽였다.
    _job(conn, "j1", sha="Y", opts="O", status="queued")
    conn.execute("UPDATE jobs SET attempts=1 WHERE id='j1'"); conn.commit()
    called = {"n": 0}
    def fake_convert(*a, **k):
        called["n"] += 1; return (0, 0)
    monkeypatch.setattr(worker.convert, "convert", fake_convert)
    monkeypatch.setattr(worker.convert, "opts_hash", lambda *a: "O")

    assert worker.process_one(conn) is True
    assert called["n"] == 0  # convert 미호출
    row = db.get_job(conn, "j1")
    assert row["status"] == "failed"
    assert "너무 무거워" in row["error"]
    row = db.get_job(conn, "j1")
    assert row["status"] == "failed"
    assert "메모리" in row["error"]


def test_sweep_deletes_expired_and_orphans(conn, monkeypatch):
    # 오래된 잡 + 그 파일
    (config.UPLOADS_DIR / "OLD.pdf").write_bytes(b"%PDF")
    old_res = config.RESULTS_DIR / "OLD-O"; old_res.mkdir()
    (old_res / "doc.md").write_text("x")
    _job(conn, "old", sha="OLD", opts="O", status="done", result_dir=str(old_res))
    # created_at을 과거로
    conn.execute("UPDATE jobs SET created_at=? WHERE id='old'",
                 (time.time() - config.RETENTION_SEC - 10,)); conn.commit()

    worker.sweep(conn)
    assert db.get_job(conn, "old") is None
    assert not (config.UPLOADS_DIR / "OLD.pdf").exists()
    assert not old_res.exists()


def test_sweep_preserves_shared_files_of_live_cachehit_job(conn):
    # 캐시 히트로 result_dir/sha256을 공유하는 최신 job이 있으면,
    # 만료된 옛 job이 지워져도 참조 카운팅으로 공유 파일은 살아남아야 한다.
    (config.UPLOADS_DIR / "SHARED.pdf").write_bytes(b"%PDF")
    shared_res = config.RESULTS_DIR / "SHARED-O"; shared_res.mkdir()
    (shared_res / "doc.md").write_text("x")

    _job(conn, "old", sha="SHARED", opts="O", status="done", result_dir=str(shared_res))
    conn.execute("UPDATE jobs SET created_at=? WHERE id='old'",
                 (time.time() - config.RETENTION_SEC - 10,)); conn.commit()

    # 캐시 히트: 같은 sha256/opts_hash/result_dir, 최근 created_at(기본값)
    _job(conn, "live", sha="SHARED", opts="O", status="done", result_dir=str(shared_res))

    worker.sweep(conn)
    assert db.get_job(conn, "old") is None
    assert db.get_job(conn, "live") is not None
    assert (config.UPLOADS_DIR / "SHARED.pdf").exists()
    assert shared_res.exists()


# --- Task 4: FastAPI 웹 ---

import zipfile
from io import BytesIO
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    _use_tmp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "ADMIN_KEY", "secret")
    config.ensure_dirs()
    db.init_db()
    from app import web
    return TestClient(web.app)


def _pdf_bytes():
    return FIX.read_bytes()


def test_index_sets_session_cookie(client):
    r1 = client.get("/")
    assert r1.status_code == 200
    sid1 = r1.cookies.get("sid")
    assert sid1

    r2 = client.get("/")  # client jar already carries sid1 from r1
    assert r2.status_code == 200
    # cookie is reused, not rotated (no new Set-Cookie changing the value)
    sid2 = r2.cookies.get("sid", sid1)
    assert sid2 == sid1


def test_upload_creates_queued_job(client):
    r = client.post("/api/jobs",
                    files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1 and jobs[0]["status"] == "queued"
    assert "sid" in r.cookies


def test_upload_rejects_non_pdf(client):
    r = client.post("/api/jobs",
                    files={"files": ("x.pdf", b"PK\x03\x04not a pdf", "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    jobs = r.json()
    assert jobs[0]["status"] == "failed"
    assert "PDF" in (jobs[0]["error"] or "")


def test_cache_hit_second_upload_skips(client):
    f = {"files": ("a.pdf", _pdf_bytes(), "application/pdf")}
    d = {"include_images": "true", "include_tables_csv": "true"}
    r1 = client.post("/api/jobs", files=f, data=d)
    # 첫 잡을 done으로 만들고 결과 디렉토리 생성
    conn = db.connect()
    j1 = r1.json()[0]
    res_dir = config.RESULTS_DIR / f"{j1['sha256']}-{j1['opts_hash']}"
    res_dir.mkdir(parents=True); (res_dir / "doc.md").write_text("# x")
    db.finish_job(conn, j1["id"], status="done", result_dir=str(res_dir),
                  n_tables=2, n_images=4)
    conn.close()
    r2 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")}, data=d)
    j2 = r2.json()[0]
    assert j2["status"] == "done"  # 캐시 히트 → 즉시 done
    # 캐시 히트 잡은 원본의 표/이미지 카운트를 복사해야 함
    assert j2["n_tables"] == 2
    assert j2["n_images"] == 4


def _seed_done(client, **opts):
    """같은 파일·옵션의 done 잡을 하나 심어 /api/convert가 캐시 히트로 즉시 끝나게 한다."""
    d = {"include_images": "false", "include_tables_csv": "false", **opts}
    j = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data=d).json()[0]
    res_dir = config.RESULTS_DIR / f"{j['sha256']}-{j['opts_hash']}"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "doc.md").write_text("# 제목\n\n본문", encoding="utf-8")
    conn = db.connect()
    db.finish_job(conn, j["id"], status="done", result_dir=str(res_dir),
                  n_tables=0, n_images=0)
    conn.close()


def test_convert_returns_markdown_body(client):
    _seed_done(client)
    r = client.post("/api/convert",
                    files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "# 제목\n\n본문"  # 마크다운 본문만, JSON 래핑 없음


def test_convert_rejects_non_pdf_with_reason(client):
    r = client.post("/api/convert",
                    files={"file": ("x.pdf", b"PK\x03\x04not a pdf", "application/pdf")})
    assert r.status_code == 422
    assert "PDF" in r.text


def test_convert_202_when_not_finished_in_time(client):
    # 워커가 없어 queued 그대로 → timeout 안에 못 끝내면 잡 id로 넘겨야 한다
    r = client.post("/api/convert",
                    files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"timeout": "0"})
    assert r.status_code == 202
    assert r.json()["job_id"]


def test_convert_needs_no_cookie_jar(client):
    _seed_done(client)
    fresh = TestClient(client.app)  # 쿠키 없는 새 클라이언트 = 외부 에이전트
    r = fresh.post("/api/convert",
                   files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200 and r.text == "# 제목\n\n본문"


def test_session_isolation_download_404(client):
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                     data={"include_images": "true", "include_tables_csv": "true"})
    jid = r1.json()[0]["id"]
    other = TestClient(client.app)  # 새 세션
    assert other.get(f"/api/jobs/{jid}/download").status_code == 404


def test_admin_key_sees_all(client):
    client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                data={"include_images": "true", "include_tables_csv": "true"})
    other = TestClient(client.app)
    r = other.get("/api/jobs", headers={"X-Admin-Key": "secret"})
    assert len(r.json()["jobs"]) >= 1


def test_jobs_response_has_busy_and_ahead(client):
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                     data={"include_images": "true", "include_tables_csv": "true"})
    conn = db.connect()
    claimed = db.claim_next_queued(conn)  # 다른 워커가 이미 하나를 실행 중이라고 가정
    assert claimed is not None
    conn.close()

    other = TestClient(client.app)  # 다른 세션
    r2 = other.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    jid2 = r2.json()[0]["id"]

    r = other.get("/api/jobs")
    body = r.json()
    assert body["busy"] is True
    mine = [j for j in body["jobs"] if j["id"] == jid2]
    assert mine and mine[0]["status"] == "queued"
    assert mine[0]["ahead"] >= 1


def test_events_disables_proxy_buffering(client):
    # 이 헤더가 빠지면 nginx 뒤에서 SSE가 버퍼링돼 진행률이 실시간으로 안 뜬다.
    import asyncio
    from app import web

    class Req:
        cookies = {"sid": "s"}
        headers = {}

    async def head():
        resp = await web.events(Req())
        await resp.body_iterator.aclose()
        return resp.headers

    assert asyncio.run(head())["x-accel-buffering"] == "no"


def test_events_sends_keepalive_when_nothing_changed(client):
    # SSE는 변경/running이 있을 때만 데이터 프레임을 보내고, 그 외엔 코멘트로 연결만
    # 유지한다(클라이언트가 매 틱 재렌더하지 않게). 첫 프레임은 busy 초기값 전달용.
    import asyncio
    from app import web

    class Req:
        cookies = {"sid": "s"}
        headers = {}

    async def first_two():
        resp = await web.events(Req())
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
            if len(out) == 2:
                break
        await resp.body_iterator.aclose()
        return out

    first, second = asyncio.run(first_two())
    assert first.startswith("data: ")
    assert second.startswith(": ")


def test_events_resends_queued_jobs_when_ahead_shrinks(client):
    # 앞선 잡이 끝나면 뒤 queued 잡의 대기 순번이 줄어든다. 그 변화도 전송되어야
    # UI의 "앞에 N개 대기"가 낡은 값으로 굳지 않는다.
    import asyncio
    import json
    from app import web

    class Req:
        cookies = {"sid": "s"}
        headers = {}

    conn = db.connect()
    for i, t in enumerate([100.0, 200.0, 300.0]):
        db.create_job(conn, id=f"j{i}", session_id="s", filename="f.pdf", sha256="a",
                      opts_hash="o", status="queued", page_total=1)
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (t, f"j{i}"))
    conn.commit()

    async def two_frames():
        resp = await web.events(Req())
        it = resp.body_iterator
        first = await it.__anext__()
        db.finish_job(conn, "j0", status="done")  # 맨 앞 잡 완료
        second = await it.__anext__()
        await it.aclose()
        return first, second

    first, second = asyncio.run(two_frames())
    conn.close()
    aheads = {j["id"]: j.get("ahead") for j in json.loads(first[6:])["jobs"]}
    assert aheads == {"j0": 0, "j1": 1, "j2": 2}
    aheads = {j["id"]: j.get("ahead") for j in json.loads(second[6:])["jobs"]}
    assert aheads["j1"] == 0 and aheads["j2"] == 1


def test_events_does_not_resend_unchanged_done_jobs(client):
    # ahead는 queued에만 의미가 있다. done 잡에도 순번을 매기면 앞의 활성 잡이 끝날
    # 때마다 값이 흔들려, 변한 게 없는 done 카드가 계속 재전송된다.
    import asyncio
    import json
    from app import web

    class Req:
        cookies = {"sid": "s"}
        headers = {}

    conn = db.connect()
    db.create_job(conn, id="q1", session_id="s", filename="a.pdf", sha256="a",
                  opts_hash="o", status="queued", page_total=1)
    conn.execute("UPDATE jobs SET created_at=100.0 WHERE id='q1'")
    db.create_job(conn, id="d1", session_id="s", filename="b.pdf", sha256="b",
                  opts_hash="o", status="done", page_total=1)  # 캐시 히트: 나중에 생성된 done
    conn.execute("UPDATE jobs SET created_at=200.0, finished_at=201.0 WHERE id='d1'")
    conn.commit()

    async def second_frame():
        resp = await web.events(Req())
        it = resp.body_iterator
        await it.__anext__()                      # 초기 스냅샷
        db.finish_job(conn, "q1", status="done")  # 유일한 활성 잡이 사라짐
        frame = await it.__anext__()
        await it.aclose()
        return frame

    frame = asyncio.run(second_frame())
    conn.close()
    ids = [j["id"] for j in json.loads(frame[6:])["jobs"]]
    assert "q1" in ids  # 실제로 바뀐 잡은 전송된다
    assert "d1" not in ids  # 변화 없는 done 잡은 전송하지 않는다


def test_download_all_zips_done_jobs(client):
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                     data={"include_images": "true", "include_tables_csv": "true"})
    j1 = r1.json()[0]
    conn = db.connect()
    res_dir = config.RESULTS_DIR / f"{j1['sha256']}-{j1['opts_hash']}"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "doc.md").write_text("# hello")
    with zipfile.ZipFile(res_dir / "result.zip", "w") as z:
        z.writestr("doc.md", "# hello")
    db.finish_job(conn, j1["id"], status="done", result_dir=str(res_dir),
                  n_tables=0, n_images=0)
    conn.close()

    r = client.get("/api/download-all")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(BytesIO(r.content))
    assert any(n.endswith("doc.md") for n in zf.namelist())

    fresh = TestClient(client.app)  # 결과 없는 새 세션
    assert fresh.get("/api/download-all").status_code == 404


def test_download_all_rejects_dotdot_filename(client):
    # filename=".." must not let a zip entry escape its folder (zip-slip).
    conn = db.connect()
    res_dir = config.RESULTS_DIR / "dotdot-O"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "doc.md").write_text("# hello")
    db.create_job(conn, id="jdd", session_id="s-dotdot", filename="..", sha256="dd",
                  opts_hash="O", status="queued", page_total=1)
    db.finish_job(conn, "jdd", status="done", result_dir=str(res_dir))
    conn.close()

    client.cookies.set("sid", "s-dotdot")
    r = client.get("/api/download-all")
    assert r.status_code == 200
    zf = zipfile.ZipFile(BytesIO(r.content))
    for n in zf.namelist():
        assert not n.startswith("../")
        assert ".." not in Path(n).parts
        assert not Path(n).is_absolute()


def test_upload_rejects_oversize(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_BYTES", 3)
    r = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    job = r.json()[0]
    assert job["status"] == "failed"
    assert "100MB" in job["error"]


def test_upload_rejects_too_many_pages(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES", 0)
    r = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    job = r.json()[0]
    assert job["status"] == "failed"
    assert "500페이지" in job["error"]


def test_upload_rejects_textless_pdf(client, monkeypatch):
    # 스캔본(이미지) PDF: OCR을 끈 파이프라인에선 예외 없이 빈 doc.md가 나온다.
    monkeypatch.setattr(config, "MIN_TEXT_CHARS", 10_000)
    r = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    job = r.json()[0]
    assert job["status"] == "failed"
    assert "스캔본" in job["error"]


def test_upload_rejects_over_queue_cap(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUED_PER_SESSION", 1)
    d = {"include_images": "true", "include_tables_csv": "true"}
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")}, data=d)
    assert r1.json()[0]["status"] == "queued"
    r2 = client.post("/api/jobs", files={"files": ("b.pdf", _pdf_bytes(), "application/pdf")}, data=d)
    job2 = r2.json()[0]
    assert job2["status"] == "failed"
    assert "대기 잡이 너무 많습니다" in job2["error"]


def test_web_self_initializes_storage_without_worker(tmp_path, monkeypatch):
    # worker가 아직 ensure_dirs()/init_db()를 실행하지 않은 상황을 재현: 이 테스트는
    # (client 픽스처와 달리) db.init_db()를 직접 호출하지 않는다. web이 자기 스토리지를
    # 스스로 초기화하지 못하면 DB 파일이 없어 /api/jobs가 500을 낸다.
    _use_tmp_data(monkeypatch, tmp_path)
    assert not (tmp_path / "app.db").exists()

    from app import web
    with TestClient(web.app) as c:  # lifespan 실행 -> ensure_dirs()+init_db()
        assert config.DB_PATH.exists()
        r = c.get("/api/jobs")
        assert r.status_code == 200
        assert r.json() == {"jobs": [], "busy": False}


def test_perf_knobs_gpu_batches_beat_cpu():
    """CPU 값(배치 1)이 GPU로 새면 카드를 한 페이지씩만 먹여 가속이 거의 사라진다."""
    cpu = convert._perf_knobs(cuda=False)
    gpu = convert._perf_knobs(cuda=True)
    assert cpu == {"queue_max_size": 2, "layout_batch_size": 1, "table_batch_size": 1}
    assert gpu["layout_batch_size"] > 1 and gpu["table_batch_size"] > 1
    assert gpu["queue_max_size"] > cpu["queue_max_size"]


def test_perf_knobs_gpu_batch_env_knob(monkeypatch):
    """VRAM이 모자랄 때 재빌드 없이 낮출 수 있어야 한다."""
    monkeypatch.setattr(convert, "GPU_BATCH", 1)
    assert convert._perf_knobs(cuda=True)["layout_batch_size"] == 1


def test_cuda_available_false_without_torch():
    """torch가 없으면 예외 대신 False → CPU 설정으로 되돌아간다(테스트가 이 경로다)."""
    assert convert._cuda_available() is False

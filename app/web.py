import os
import re
import time
import uuid
import json
import hmac
import asyncio
import zipfile
import tempfile
from bisect import bisect_left
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import (
    JSONResponse, PlainTextResponse, FileResponse, StreamingResponse, HTMLResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import config, db, convert

STATIC = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # web과 worker는 도커 컴포즈에서 별도 컨테이너로 떠서 시작 순서가 보장되지 않는다.
    # worker가 아직 안 떴어도 web이 바로 200을 내야 하므로, 스토리지(데이터 디렉터리 +
    # SQLite 스키마) 초기화를 worker에 의존하지 않고 web이 직접 수행한다.
    config.ensure_dirs()
    db.init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _sid(request: Request) -> str:
    return request.cookies.get("sid") or uuid.uuid4().hex


def _is_admin(request: Request) -> bool:
    key = request.headers.get("X-Admin-Key", "")
    return bool(config.ADMIN_KEY) and hmac.compare_digest(key, config.ADMIN_KEY)


def _serialize(actives, r) -> dict:
    """행 직렬화 + 진행률(추정) + queued 잡의 전역 대기 순번(ahead).

    actives: db.active_created_ats()의 오름차순 목록.
    """
    d = dict(r)
    if d["status"] == "done":
        d["progress"] = 100
    elif d["status"] == "running" and d.get("started_at"):
        est = max(1.0, (d.get("page_total") or 1) * config.SEC_PER_PAGE)
        d["progress"] = min(95, int((time.time() - d["started_at"]) / est * 100))
    else:
        d["progress"] = 0
    if d["status"] == "queued":
        d["ahead"] = bisect_left(actives, r["created_at"])
    d.pop("session_id", None)
    d.pop("result_dir", None)  # 서버 절대경로, UI/테스트 모두 미사용
    return d


def _set_sid_cookie(response, sid: str) -> None:
    response.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=30 * 86400)


def _safe_name(name: str) -> str:
    # ponytail: 경로 구분자만 제거하는 단순 화이트리스트, 유니코드 파일명은 그대로 둠
    name = Path(name).name
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    if not name or re.fullmatch(r"\.+", name):
        name = "file"
    return name


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    response = HTMLResponse(html)
    # sid를 SSE 연결 전에 미리 발급해야, 첫 방문자의 GET /events가 나중에 업로드로
    # 재발급되는 sid와 어긋나서 진행률이 안 보이는 문제가 없다.
    sid = request.cookies.get("sid")
    if not sid:
        _set_sid_cookie(response, uuid.uuid4().hex)
    return response


_TOO_BIG = f"파일이 {config.MAX_BYTES // (1024 * 1024)}MB를 초과합니다"
_TOO_MANY_PAGES = f"{config.MAX_PAGES}페이지를 초과합니다"
_TOO_MANY_QUEUED = f"대기 잡이 너무 많습니다(최대 {config.MAX_QUEUED_PER_SESSION})"
_BROKEN_PDF = "PDF를 열 수 없습니다(손상되었거나 암호로 보호된 파일)"
_EMPTY_PDF = "페이지가 없는 PDF입니다"
_NO_TEXT = "텍스트 레이어가 없습니다 — 스캔본(이미지) PDF는 OCR 미지원"


def _fail(conn, jid, sid, filename, oh, error, *, sha="-", page_total=None):
    db.create_job(conn, id=jid, session_id=sid, filename=filename,
                  sha256=sha, opts_hash=oh, status="failed", page_total=page_total)
    db.finish_job(conn, jid, status="failed", error=error)
    return db.get_job(conn, jid)


@app.post("/api/jobs")
async def create_jobs(request: Request,
                      files: Optional[list[UploadFile]] = None,
                      include_images: str = Form("false"),
                      include_tables_csv: str = Form("false")):
    sid = _sid(request)
    inc_img = include_images == "true"
    inc_csv = include_tables_csv == "true"
    oh = convert.opts_hash(inc_img, inc_csv)
    conn = db.connect()
    out = []
    try:
        for uf in (files or []):
            jid = uuid.uuid4().hex
            # 가드레일: Starlette가 파트의 Content-Length로 채워주는 uf.size를 먼저
            # 확인해 초대형 업로드를 메모리에 통째로 읽기 전에 걸러낸다(OOM 방지).
            # uf.size가 없는(None) 경우에만 아래 read() 이후 len(data) 체크가 백스톱.
            if uf.size is not None and uf.size > config.MAX_BYTES:
                out.append(_fail(conn, jid, sid, uf.filename, oh, _TOO_BIG)); continue
            data = await uf.read()
            if len(data) > config.MAX_BYTES:
                out.append(_fail(conn, jid, sid, uf.filename, oh, _TOO_BIG)); continue
            if not convert.is_pdf(data[:5]):
                out.append(_fail(conn, jid, sid, uf.filename, oh,
                                 "PDF 파일이 아닙니다")); continue
            if db.count_queued(conn, sid) >= config.MAX_QUEUED_PER_SESSION:
                out.append(_fail(conn, jid, sid, uf.filename, oh, _TOO_MANY_QUEUED)); continue

            sha = convert.sha256_bytes(data)
            pdf_path = config.UPLOADS_DIR / f"{sha}.pdf"
            if not pdf_path.exists():
                pdf_path.write_bytes(data)
            try:
                pages, text_chars = convert.probe(pdf_path)
            except Exception:
                out.append(_fail(conn, jid, sid, uf.filename, oh,
                                 _BROKEN_PDF, sha=sha)); continue
            if pages == 0:
                out.append(_fail(conn, jid, sid, uf.filename, oh,
                                 _EMPTY_PDF, sha=sha)); continue
            if pages > config.MAX_PAGES:
                out.append(_fail(conn, jid, sid, uf.filename, oh,
                                 _TOO_MANY_PAGES, sha=sha, page_total=pages)); continue
            # 스캔본은 변환이 예외 없이 '성공'하고 빈 doc.md를 남긴다. 몇 분 기다린 끝에
            # 빈 결과를 받지 않도록 업로드 시점에 거른다.
            if text_chars < config.MIN_TEXT_CHARS:
                out.append(_fail(conn, jid, sid, uf.filename, oh,
                                 _NO_TEXT, sha=sha, page_total=pages)); continue

            cached = db.find_cached(conn, sha, oh)
            if cached:
                db.create_job(conn, id=jid, session_id=sid, filename=uf.filename,
                              sha256=sha, opts_hash=oh, status="done", page_total=pages,
                              result_dir=cached["result_dir"])
                # 캐시 히트: 원본의 표/이미지 카운트를 그대로 복사
                db.finish_job(conn, jid, status="done", result_dir=cached["result_dir"],
                              n_tables=cached["n_tables"], n_images=cached["n_images"])
            else:
                db.create_job(conn, id=jid, session_id=sid, filename=uf.filename,
                              sha256=sha, opts_hash=oh, status="queued", page_total=pages)
            out.append(db.get_job(conn, jid))
        actives = db.active_created_ats(conn)
        result = [_serialize(actives, r) for r in out]
    finally:
        conn.close()
    response = JSONResponse(result)
    _set_sid_cookie(response, sid)
    return response


@app.post("/api/convert")
async def convert_sync(request: Request,
                       file: UploadFile,
                       include_images: str = Form("false"),
                       include_tables_csv: str = Form("false"),
                       timeout: float = Form(300)):
    """PDF 1개 → 마크다운 본문(text/plain). 외부 에이전트용 한 방 엔드포인트.

    업로드 검증·해시캐시·큐는 create_jobs를 그대로 재사용하고, 여기서는 완료를
    기다렸다가 doc.md만 돌려준다 — 호출자는 쿠키 세션도 폴링도 구현할 필요가 없다.
    include_images 기본값이 false인 이유: true면 doc.md에 `images/...` 상대경로가
    남는데, 본문만 가져가는 호출자에게는 받을 수 없는 깨진 링크다.
    """
    resp = await create_jobs(request, files=[file],
                             include_images=include_images,
                             include_tables_csv=include_tables_csv)
    job_id = json.loads(resp.body)[0]["id"]
    conn = db.connect()
    try:
        deadline = time.time() + timeout
        while True:
            row = db.get_job(conn, job_id)
            if row["status"] == "done":
                md = Path(row["result_dir"]) / "doc.md"
                return PlainTextResponse(md.read_text(encoding="utf-8"))
            if row["status"] == "failed":
                return PlainTextResponse(row["error"] or "변환 실패", status_code=422)
            # 상태 확인을 먼저 하고 마감을 보므로, 캐시 히트나 업로드 검증 실패는
            # timeout=0이어도 즉시 제 응답으로 나간다.
            if time.time() >= deadline:
                break
            await asyncio.sleep(0.5)
    finally:
        conn.close()
    # 아직 처리 중 — 커넥션을 더 붙들지 않고 잡 id로 넘긴다. 회수는
    # GET /api/jobs/{id}/preview (create_jobs가 발급한 sid 쿠키 또는 X-Admin-Key 필요).
    accepted = JSONResponse({"job_id": job_id, "status": "running"}, status_code=202)
    cookie = resp.headers.get("set-cookie")
    if cookie:
        accepted.headers.append("set-cookie", cookie)
    return accepted


@app.get("/api/jobs")
def list_jobs(request: Request):
    conn = db.connect()
    try:
        rows = db.list_jobs(conn, _sid(request), admin=_is_admin(request))
        actives = db.active_created_ats(conn)
        return JSONResponse({
            "jobs": [_serialize(actives, r) for r in rows],
            "busy": db.worker_busy(conn),
        })
    finally:
        conn.close()


def _owned(request, job_id):
    conn = db.connect()
    try:
        row = db.get_job(conn, job_id)
        if row is None:
            return None
        if row["session_id"] != _sid(request) and not _is_admin(request):
            return None
        return row
    finally:
        conn.close()


@app.get("/api/jobs/{job_id}/preview")
def preview(request: Request, job_id: str):
    row = _owned(request, job_id)
    if not row or row["status"] != "done":
        return PlainTextResponse("not found", status_code=404)
    md = Path(row["result_dir"]) / "doc.md"
    if not md.exists():
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(md.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/download")
def download(request: Request, job_id: str):
    row = _owned(request, job_id)
    if not row or row["status"] != "done":
        return PlainTextResponse("not found", status_code=404)
    zp = Path(row["result_dir"]) / "result.zip"
    if not zp.exists():
        return PlainTextResponse("not found", status_code=404)
    name = Path(row["filename"]).stem + ".zip"
    return FileResponse(zp, filename=name, media_type="application/zip")


@app.get("/api/download-all")
def download_all(request: Request):
    conn = db.connect()
    try:
        rows = db.list_jobs(conn, _sid(request), admin=_is_admin(request))
        done = [r for r in rows if r["status"] == "done" and r["result_dir"]]
    finally:
        conn.close()
    if not done:
        return PlainTextResponse("no results", status_code=404)

    used = {}
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as z:
            for r in done:
                src = Path(r["result_dir"])
                if not src.exists():
                    continue
                base = _safe_name(Path(r["filename"]).stem)
                n = used.get(base, 0)
                used[base] = n + 1
                folder = base if n == 0 else f"{base}-{n}"
                for f in sorted(src.rglob("*")):
                    if f.is_file():
                        z.write(f, f"{folder}/{f.relative_to(src)}")
    except Exception:
        os.unlink(tmp_name)
        raise

    return FileResponse(
        tmp_name, filename="pdf2md-변환결과.zip", media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp_name),
    )


@app.get("/api/events")
async def events(request: Request):
    sid = _sid(request)
    admin = _is_admin(request)

    async def gen():
        # 동기 def + time.sleep이면 Starlette가 이 장수(long-lived) 제너레이터를
        # AnyIO 스레드풀(기본 ~40개)에서 돌려, 열린 탭 수만큼 스레드를 영구 점유해
        # 업로드/다운로드 같은 다른 동기 엔드포인트를 굶길 수 있다. async+await로
        # 이벤트 루프에 맡긴다. 내부 sqlite 읽기는 매우 빨라(수 ms 이하) 루프를
        # 잠깐 블로킹해도 이 내부용 도구 규모에서는 감수 가능한 트레이드오프.
        seen = {}
        last_busy = None
        conn = db.connect()
        try:
            for _ in range(600):  # 최대 5분 후 재연결 유도
                rows = db.list_jobs(conn, sid, admin=admin)
                busy = db.worker_busy(conn)
                actives = db.active_created_ats(conn)
                payload = {}
                for r in rows:
                    # ahead를 키에 포함해야, 앞선 잡이 끝나 대기 순번이 줄었을 때 그 뒤
                    # queued 잡들도 갱신 프레임을 받는다. _serialize와 같이 queued에만
                    # 계산한다 — done 잡에도 매기면 활성 잡이 빠질 때마다 값이 흔들려
                    # 변한 게 없는 카드가 계속 재전송된다.
                    ahead = bisect_left(actives, r["created_at"]) if r["status"] == "queued" else 0
                    key = (r["status"], r["finished_at"], ahead)
                    # running 잡은 진행률이 계속 변하므로 변경 여부와 무관하게 항상 포함
                    if seen.get(r["id"]) != key or r["status"] == "running":
                        seen[r["id"]] = key
                        payload[r["id"]] = _serialize(actives, r)
                if payload or busy != last_busy:
                    last_busy = busy
                    yield f"data: {json.dumps({'jobs': list(payload.values()), 'busy': busy})}\n\n"
                else:
                    yield ": keepalive\n\n"  # 변화 없음 — 클라이언트 재렌더 없이 연결만 유지
                await asyncio.sleep(0.5)
        finally:
            conn.close()

    # X-Accel-Buffering: nginx는 프록시 응답을 기본으로 버퍼링해서, 0.5초 push가
    # 뭉쳐 오다가 잡이 끝나야 한꺼번에 도착한다(화면상 "멈춘 것처럼" 보인다).
    # 프록시 쪽 proxy_buffering off에 의존하지 않도록 앱이 직접 끈다.
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})

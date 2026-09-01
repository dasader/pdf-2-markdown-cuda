import shutil
import time
import traceback
from pathlib import Path

from app import config, db, convert

_SWEEP_EVERY = 300  # 초
# 워커를 한 번이라도 죽인 문서는 재시도하지 않는다. 저사양 재시도(그림 크롭 off)를
# 두던 자리인데, 실측상 메모리를 전혀 못 줄여(6.1GB → 6.2GB) 워커만 한 번 더 죽었다.
# 메모리 폭발의 주범은 그림 크롭이 아니라 백엔드의 페이지 비트맵 파싱이다.
_MAX_ATTEMPTS = 1


def process_one(conn) -> bool:
    job = db.claim_next_queued(conn)
    if job is None:
        return False
    attempts = job["attempts"] or 0
    if attempts >= _MAX_ATTEMPTS:
        # 이 문서는 이미 워커를 죽였다(OOM). 다시 시도해도 같은 결과다.
        db.finish_job(conn, job["id"], status="failed",
                      error="문서가 너무 무거워 변환하지 못했습니다. 페이지에 이미지·도형이 "
                            "과도하게 많으면 메모리 한도를 넘길 수 있습니다.")
        return True
    sha, opts = job["sha256"], job["opts_hash"]
    pdf_path = config.UPLOADS_DIR / f"{sha}.pdf"
    out_dir = config.RESULTS_DIR / f"{sha}-{opts}"
    # ponytail: opts 4조합 역산, 조합이 늘면 컬럼 추가
    pair = next(((i, c) for i in (True, False) for c in (True, False)
                 if convert.opts_hash(i, c) == opts), None)
    if pair is None:
        # CONVERTER_REV가 올라간 뒤 남아있던 옛 queued 잡. 옵션을 복원할 수 없으므로
        # 조용히 기본값으로 변환해 요청과 다른 결과를 내보내는 대신 실패시킨다.
        db.finish_job(conn, job["id"], status="failed",
                      error="변환기가 업데이트되었습니다. 다시 업로드해 주세요.")
        return True
    include_images, include_csv = pair
    try:
        n_tables, n_images = convert.convert(
            pdf_path, out_dir, include_images=include_images,
            include_tables_csv=include_csv)
        db.finish_job(conn, job["id"], status="done", result_dir=str(out_dir),
                       n_tables=n_tables, n_images=n_images)
    except Exception:
        err = traceback.format_exc(limit=3)
        db.finish_job(conn, job["id"], status="failed", error=err)
        # CUDA OOM은 호스트 RAM OOM과 달리 프로세스를 죽이지 않고 살려두는데, 그
        # 상태의 할당자에 남은 조각 때문에 이후 잡이 줄줄이 같은 오류로 실패한다.
        # 워커가 조용히 무용지물이 되는 것보다 죽는 게 낫다 —
        # restart: unless-stopped가 깨끗한 CUDA 컨텍스트로 되살린다.
        if "CUDA out of memory" in err:
            raise SystemExit(1)
    return True


def sweep(conn) -> None:
    db.delete_expired(conn)
    # 고아 파일 정리: 참조되지 않는 upload/result 삭제
    keep_shas = db.referenced_shas(conn)
    for f in config.UPLOADS_DIR.glob("*.pdf"):
        if f.stem not in keep_shas:
            f.unlink(missing_ok=True)
    keep_dirs = {Path(d).name for d in db.referenced_result_dirs(conn)}
    for d in config.RESULTS_DIR.iterdir():
        if d.is_dir() and d.name not in keep_dirs:
            shutil.rmtree(d, ignore_errors=True)


def run() -> None:
    config.ensure_dirs()
    db.init_db()
    conn = db.connect()
    db.requeue_running(conn)  # 이전 실행에서 죽은 running 잡 복구 (워커는 하나뿐)
    last_sweep = 0.0
    while True:
        worked = process_one(conn)
        now = time.time()
        if now - last_sweep > _SWEEP_EVERY:
            sweep(conn)
            last_sweep = now
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    run()

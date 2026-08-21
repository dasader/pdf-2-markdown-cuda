import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PDF2MD_DATA", "data"))
DB_PATH = DATA_DIR / "app.db"
UPLOADS_DIR = DATA_DIR / "uploads"
RESULTS_DIR = DATA_DIR / "results"

ADMIN_KEY = os.environ.get("PDF2MD_ADMIN_KEY", "")

MAX_BYTES = 100 * 1024 * 1024
# 실측(940p·15MB 텍스트 PDF, 이미지·CSV 끔): peak 3.18GB / 1512초. worker mem_limit
# 5g 안에 든다 — queue_max_size=2와 그림 크롭 skip 덕에 메모리가 페이지 수에 거의
# 비례하지 않는다(100p 1.87GB → 940p 3.18GB). 다만 "이미지 포함"을 켜면 크롭이
# 살아나 178p에서 +73%였으므로, 1000p에 가까운 문서로 켜면 5g를 넘겨 OOM이 날 수 있다.
# ponytail: 환경변수 손잡이, 호스트 RAM이 다르면 여기만 낮춘다.
MAX_PAGES = int(os.environ.get("PDF2MD_MAX_PAGES", "1000"))
MAX_QUEUED_PER_SESSION = 20
# 표본 페이지의 텍스트가 이보다 적으면 스캔본(이미지) PDF로 본다. OCR을 끈 파이프라인
# 이라 스캔본은 빈 doc.md로 '성공'해버린다. 페이지 번호만 찍힌 스캔본까지 잡되 짧은
# 정상 문서는 통과시키는 선. ponytail: 실측으로 조정하는 손잡이, 자동 판별은 과잉.
MIN_TEXT_CHARS = int(os.environ.get("PDF2MD_MIN_TEXT_CHARS", "10"))
SEC_PER_PAGE = float(os.environ.get("PDF2MD_SEC_PER_PAGE", "1.5"))
RETENTION_SEC = 24 * 3600


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOADS_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

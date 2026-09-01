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
# GPU 오버레이는 queue_max_size가 16(CPU는 2)이라 인플라이트 페이지 비트맵이 더 쌓인다
# — 1000p는 GPU 경로에서 미실측이다. PDF2MD_WORKER_MEM을 8g로 낮춘 랩탑이면 여기도 낮춘다.
# ponytail: 환경변수 손잡이, 호스트 RAM이 다르면 여기만 낮춘다.
MAX_PAGES = int(os.environ.get("PDF2MD_MAX_PAGES", "1000"))
MAX_QUEUED_PER_SESSION = 20
# 표본 페이지의 텍스트가 이보다 적으면 스캔본(이미지) PDF로 본다. OCR을 끈 파이프라인
# 이라 스캔본은 빈 doc.md로 '성공'해버린다. 페이지 번호만 찍힌 스캔본까지 잡되 짧은
# 정상 문서는 통과시키는 선. ponytail: 실측으로 조정하는 손잡이, 자동 판별은 과잉.
MIN_TEXT_CHARS = int(os.environ.get("PDF2MD_MIN_TEXT_CHARS", "10"))

# OCR·차트 추출을 UI에 노출할지. web 컨테이너에는 GPU가 붙지 않아 스스로 판별할 수
# 없으므로, docker-compose.gpu.yml이 web·worker 양쪽에 넣어주는 값을 본다. 워커는
# 이 값을 믿지 않고 실제 torch.cuda로 한 번 더 확인하고, 어긋나면 잡을 실패시킨다
# (조용히 OCR 없이 변환해 빈 결과를 '성공'으로 돌려주는 게 제일 나쁘다).
GPU_ENABLED = os.environ.get("PDF2MD_GPU", "") == "1"
# 차트→표 추출 모델. granite-vision(2B, 가중치 6.2GB)이 기본이고 VRAM 8GB에서
# 빠듯하게 돈다. granite-vision-v4(4B, 8.0GB)는 12GB 이상에서만 권한다.
# 바꾸면 Dockerfile이 굽는 모델도 같이 바꿔야 한다 — 런타임은 인터넷을 쓰지 않는다.
CHART_MODEL = os.environ.get("PDF2MD_CHART_MODEL", "granite-vision")
SEC_PER_PAGE = float(os.environ.get("PDF2MD_SEC_PER_PAGE", "1.5"))
RETENTION_SEC = 24 * 3600


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOADS_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

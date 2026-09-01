FROM python:3.12-slim

# docling 런타임(opencv)에 필요한 최소 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 사내 프록시가 TLS를 가로채는 망(인증서 체인에 self-signed CA)에서는 아래 모델
# 다운로드가 CERTIFICATE_VERIFY_FAILED로 죽는다. 루트 CA(.crt)를 certs/에 넣어두면
# 신뢰한다. 비어 있으면 아무 일도 안 한다 — 평범한 망에서는 신경 쓸 것 없다.
# certifi에도 덧붙이는 이유: httpx/huggingface_hub는 OS 신뢰 저장소가 아니라
# certifi 번들만 본다(update-ca-certificates만으로는 안 먹는다).
COPY certs/ /usr/local/share/ca-certificates/
RUN echo "사내 CA: $(ls /usr/local/share/ca-certificates/*.crt 2>/dev/null | wc -l)개" && \
    if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
      update-ca-certificates && \
      cat /usr/local/share/ca-certificates/*.crt >> "$(python -c 'import certifi; print(certifi.where())')"; \
    fi

# Docling 모델을 빌드 타임에 받아 이미지에 상주시킨다. 런타임에서 app/convert.py가
# artifacts_path로 이 디렉터리를 가리키므로 변환은 인터넷 없이 돈다.
# `|| true`를 붙이지 않는다 — 붙여뒀더니 다운로드가 실패한 이미지가 조용히 만들어지고
# 그 사실이 잡마다 터지는 런타임 오류로만 드러났다. 빌드에서 시끄럽게 죽는 게 낫다.
# 안 받는 것: RapidOCR(99MB, 한글 미지원이라 EasyOCR로 대체), 코드·수식(611MB),
# 그림 분류(33MB) — 이 파이프라인이 켜지 않는 모델이라 이미지만 키운다.
# 받는 것: 레이아웃 + TableFormer(기본), 그리고 아래 두 줄로 OCR·차트 모델.
#   - EasyOCR korean_g2 + english_g2 + craft 검출기(114MB). download_models()의
#     with_easyocr은 영문·라틴만 받으므로 언어를 직접 지정해 따로 부른다.
#   - granite-vision 2B chart2csv(가중치 6.2GB) — 차트 그림을 수치 표로 되살린다.
#     v4(4B, 8.0GB)가 더 정확하지만 VRAM 12GB 이상을 요구해 기본에서 뺐다.
#     바꾸려면 with_granite_chart_extraction_v4=True로 하고 PDF2MD_CHART_MODEL도
#     granite-vision-v4로 맞춘다.
# ponytail: 모델을 다 굽는 대신 런타임 다운로드로 미루면 이미지가 6GB 작아지지만,
# 이 서비스의 "런타임 인터넷 불필요"(make check-offline)가 깨진다.
# 실패하면 안내를 붙여 죽는다 — huggingface_hub의 기본 메시지는 100줄 트레이스백
# 끝에 "인터넷 연결을 확인하세요"라 사내망에서는 틀린 안내다.
RUN python -c "from docling.utils.model_downloader import download_models; \
    download_models(with_rapidocr=False, with_code_formula=False, \
                    with_picture_classifier=False, with_granite_chart_extraction=True); \
    from docling.datamodel.settings import settings; \
    from docling.models.stages.ocr.easyocr_model import EasyOcrModel; \
    EasyOcrModel.download_models(detection_models=['craft'], \
        recognition_models=['korean_g2', 'english_g2'], \
        local_dir=settings.cache_dir / 'models' / EasyOcrModel._model_repo_folder)" \
  || { echo ""; \
       echo "=================================================================="; \
       echo " 모델 다운로드 실패."; \
       echo " 이 이미지에 설치된 사내 CA: $(ls /usr/local/share/ca-certificates/*.crt 2>/dev/null | wc -l)개"; \
       echo ""; \
       echo " 위가 0개이고 오류가 CERTIFICATE_VERIFY_FAILED 라면 프록시가 TLS를"; \
       echo " 가로채는 망이다. certs/ 가 비어 있다는 뜻이므로 호스트에서:"; \
       echo ""; \
       echo "     make ca      # 가로채는 인증서를 certs/ 에 담는다"; \
       echo "     make gpu     # 다시 빌드 (CPU 호스트면 make build)"; \
       echo ""; \
       echo " 1개 이상인데도 실패하면 그 인증서로는 부족하다 — README의"; \
       echo " docker save 경로를 쓴다."; \
       echo "=================================================================="; \
       exit 1; }

COPY app/ app/
COPY static/ static/

ENV PDF2MD_DATA=/data
EXPOSE 8001
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8001"]

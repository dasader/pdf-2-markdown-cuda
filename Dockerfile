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
RUN if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
      update-ca-certificates && \
      cat /usr/local/share/ca-certificates/*.crt >> "$(python -c 'import certifi; print(certifi.where())')"; \
    fi

# Docling 모델을 빌드 타임에 받아 이미지에 상주시킨다. 런타임에서 app/convert.py가
# artifacts_path로 이 디렉터리를 가리키므로 변환은 인터넷 없이 돈다.
# `|| true`를 붙이지 않는다 — 붙여뒀더니 다운로드가 실패한 이미지가 조용히 만들어지고
# 그 사실이 잡마다 터지는 런타임 오류로만 드러났다. 빌드에서 시끄럽게 죽는 게 낫다.
# 안 받는 것: OCR(do_ocr=False, 99MB), 코드·수식(611MB), 그림 분류(33MB) — 이
# 파이프라인이 켜지 않는 모델이라 받아봐야 이미지만 키우고 빌드 실패 지점만 늘린다.
# ponytail: 나중에 do_ocr이나 enrichment를 켜면 여기 플래그도 같이 켜야 한다.
RUN python -c "from docling.utils.model_downloader import download_models; \
    download_models(with_rapidocr=False, with_code_formula=False, with_picture_classifier=False)"

COPY app/ app/
COPY static/ static/

ENV PDF2MD_DATA=/data
EXPOSE 8001
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8001"]

.PHONY: build rebuild gpu check-offline ca

# up -d --build 하나가 빌드+기동을 다 한다. mem_limit이 바뀌면 컨테이너도 재생성된다.
# Dockerfile이 app/·static/을 COPY하므로 코드가 바뀌면 이미지 해시가 바뀌어 컨테이너도
# 자동으로 재생성된다 — --force-recreate 불필요.
build:
	git pull
	docker compose up -d --build

rebuild: build

# GPU 호스트(3060 Ti). 오버레이를 얹어 worker에만 카드를 붙인다.
# 실제로 붙었는지는 `docker compose logs worker | grep device=` 로 확인한다.
gpu:
	git pull
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

# 이미지가 자족적인지(인터넷 없이 변환되는지) 확인한다. 한때 빌드 타임에 받아둔
# 1.3GB 모델을 런타임이 쳐다보지도 않고 잡마다 HF를 다시 쳤다 — 망이 막히거나
# 사내 프록시가 TLS를 가로채는 곳에서는 모든 변환이 실패했다. --network none이
# 그걸 잡아내는 유일한 방법이다(망 있는 개발기에서는 조용히 통과해버린다).
check-offline:
	docker run --rm --network none -v "$(CURDIR)/tests:/t:ro" -w /srv pdf2md:latest \
	  python -c "from app import convert; import tempfile, pathlib; \
	    d = pathlib.Path(tempfile.mkdtemp()); convert.convert('/t/fixtures/sample.pdf', d, include_images=True, include_tables_csv=True); \
	    assert (d / 'doc.md').exists(); print('OK: 인터넷 없이 변환됨')"

# 빌드가 CERTIFICATE_VERIFY_FAILED(self-signed certificate in certificate chain)로
# 죽을 때 쓴다. 사내 프록시가 TLS를 가로채는 망이라는 뜻이고, huggingface.co가
# 내미는 인증서 체인을 그대로 certs/ 에 담아두면 빌드가 그걸 신뢰한다.
# 발급자를 같이 찍는다 — Amazon/DigiCert 류면 정상 망이고(그럼 원인은 TLS가 아니다),
# 사내 어플라이언스 이름이 나오면 그놈이 범인이다.
# 루트가 체인에 안 실려와도 된다: OpenSSL은 신뢰 저장소에 있는 중간 인증서에서도
# 경로 검증을 끝낸다.
ca:
	@openssl s_client -showcerts -connect huggingface.co:443 </dev/null 2>/dev/null \
	  | awk '/BEGIN CERT/,/END CERT/' > certs/proxy-chain.crt
	@test -s certs/proxy-chain.crt || { rm -f certs/proxy-chain.crt; \
	  echo "체인을 못 받았다 — huggingface.co가 통째로 막힌 망이다. README의 docker save 경로를 쓴다."; \
	  exit 1; }
	@openssl crl2pkcs7 -nocrl -certfile certs/proxy-chain.crt \
	  | openssl pkcs7 -print_certs -noout | grep issuer=
	@echo "→ certs/proxy-chain.crt 저장 완료. 이제 make gpu 로 다시 빌드한다."

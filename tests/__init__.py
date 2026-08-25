"""trust-no-internet 단위테스트.

실행: python -m unittest discover -s tests -v   (프로젝트 루트에서)

외부 네트워크·API 키 없이 순수 로직만 검증한다. 대상 선정 기준은
"이게 깨지면 어떤 숫자가 거짓이 되는가" — healthcheck와 같다.
"""

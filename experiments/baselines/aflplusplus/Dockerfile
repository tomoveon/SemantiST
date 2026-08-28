ARG AFLPLUSPLUS_IMAGE=aflplusplus/aflplusplus:v4.21c@sha256:316f515ded116684a0e7a55cdbb094f6dcdd0b16f040bcf368c193824180b291
FROM ${AFLPLUSPLUS_IMAGE}

LABEL org.opencontainers.image.title="SemantiST AFL++ baseline environment" \
      org.opencontainers.image.source="https://github.com/AFLplusplus/AFLplusplus" \
      org.opencontainers.image.version="4.21c" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV AFL_SKIP_CPUFREQ=1 \
    AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1

WORKDIR /work

RUN command -v afl-fuzz \
    && command -v afl-clang-fast \
    && command -v afl-showmap \
    && afl-clang-fast --version >/dev/null

CMD ["/bin/bash"]

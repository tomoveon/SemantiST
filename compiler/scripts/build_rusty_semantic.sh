#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REVISION="be1de6f175ec1bed7928904dfb694208a24043fb"
SOURCE="${SEMANTIST_RUSTY_SOURCE:-/opt/rusty}"
DESTINATION="${SEMANTIST_RUSTY_SEMANTIC_DIR:-${ROOT}/artifacts/rusty-semantic}"
PATCH_DIR="${ROOT}/compiler/toolchain/rusty-patches"
# The patches form an ordered series: 0001 semantic metadata, 0002 ADR integer
# arithmetic fix, 0003 dialect switch for byte pointer arithmetic, 0004 loop
# temporaries of FOR/REPEAT inside an ACTION, 0005 C ABI extension of narrow
# integer arguments, 0006 forwarding of ARRAY[*] parameters.
mapfile -t PATCHES < <(find "${PATCH_DIR}" -maxdepth 1 -name '*.patch' | sort)
PROFILE="${SEMANTIST_RUSTY_PROFILE:-release}"
CARGO_OUTPUT="${SEMANTIST_RUSTY_CARGO_TARGET_DIR:-${DESTINATION}/target}"
PATCH_SHA="$(cat "${PATCHES[@]}" | sha256sum | awk '{print $1}')"
STAMP="${DESTINATION}/.semantist-semantic-patch.sha256"

clone_destination() {
  mkdir -p "$(dirname "${DESTINATION}")"
  git clone --no-hardlinks "${SOURCE}" "${DESTINATION}"
  git -C "${DESTINATION}" checkout --detach "${REVISION}"
}

worktree_clean() {
  [[ -z "$(git -C "${DESTINATION}" status --porcelain --untracked-files=all -- . ':!.semantist-semantic-patch.sha256')" ]]
}

if [[ -d "${DESTINATION}/.git" ]]; then
  installed_patch_sha=""
  [[ -f "${STAMP}" ]] && installed_patch_sha="$(tr -d '[:space:]' < "${STAMP}")"
  if [[ "${installed_patch_sha}" != "${PATCH_SHA}" ]] && ! worktree_clean; then
    backup="${DESTINATION}.stale.$(date +%s)"
    echo "[build_rusty_semantic] preserving managed clone with a different patch set at ${backup}"
    mv "${DESTINATION}" "${backup}"
  fi
fi

if [[ ! -d "${DESTINATION}/.git" ]]; then
  clone_destination
fi

actual_revision="$(git -C "${DESTINATION}" rev-parse HEAD)"
if [[ "${actual_revision}" != "${REVISION}" ]]; then
  echo "[build_rusty_semantic] expected ${REVISION}, found ${actual_revision}" >&2
  exit 1
fi

if [[ -f "${STAMP}" && "$(tr -d '[:space:]' < "${STAMP}")" == "${PATCH_SHA}" ]]; then
  echo "[build_rusty_semantic] patch series already applied"
else
  for patch in "${PATCHES[@]}"; do
    if ! git -C "${DESTINATION}" apply --check "${patch}"; then
      echo "[build_rusty_semantic] worktree is not compatible with ${patch}" >&2
      exit 1
    fi
    git -C "${DESTINATION}" apply "${patch}"
    echo "[build_rusty_semantic] applied $(basename "${patch}")"
  done
fi
printf '%s\n' "${PATCH_SHA}" > "${STAMP}"

export LLVM_SYS_211_PREFIX="${LLVM_SYS_211_PREFIX:-/usr/lib/llvm-21}"
export LLVM_CONFIG="${LLVM_CONFIG:-${LLVM_SYS_211_PREFIX}/bin/llvm-config}"
export PATH="${LLVM_SYS_211_PREFIX}/bin:${PATH}"
export CARGO_TARGET_DIR="${CARGO_OUTPUT}"

if [[ ! -x "${LLVM_CONFIG}" ]]; then
  echo "[build_rusty_semantic] missing LLVM_CONFIG: ${LLVM_CONFIG}" >&2
  exit 1
fi

LLVM_MAJOR="$("${LLVM_CONFIG}" --version | cut -d. -f1)"
if [[ "${LLVM_MAJOR}" != "21" ]]; then
  echo "[build_rusty_semantic] LLVM 21 required, found $("${LLVM_CONFIG}" --version)" >&2
  exit 1
fi


if [[ "${PROFILE}" == "release" ]]; then
  cargo build --manifest-path "${DESTINATION}/Cargo.toml" --release --locked --bin plc
else
  cargo build --manifest-path "${DESTINATION}/Cargo.toml" --locked --bin plc
fi

echo "${CARGO_OUTPUT}/${PROFILE}/plc"

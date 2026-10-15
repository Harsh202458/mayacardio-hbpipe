#!/bin/sh
# Build the submission zip:  sh tools/make_submission.sh OUT_DIR [VIDEO_FILE]
#   1_code/            clean copy of the repository (git archive of HEAD)
#   2_Design_Note.pdf  (rendered from docs/design-note.html)
#   3_evidence/        failure-test results (summary.json + logs; large exports left out)
#   4_resources/       1 h resource figures, measured on the ARM64 device against the cloud
#   5_demo_video.mp4   (if given)
#   README.md          index for the reviewers
set -eu
OUT="${1:?usage: make_submission.sh OUT_DIR [VIDEO]}"
VIDEO="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$OUT/MayaCardio_Submission"
rm -rf "$PKG" "$OUT/MayaCardio_Submission.zip"
mkdir -p "$PKG/1_code" "$PKG/3_evidence" "$PKG/4_resources"

git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$PKG/1_code"
cp "$ROOT/docs/Design_Note.pdf" "$PKG/2_Design_Note.pdf"
cp "$ROOT/docs/SUBMISSION.md" "$PKG/README.md"
cp "$ROOT/docs/EVIDENCE.md" "$PKG/3_evidence/EVIDENCE.md"
# Evidence: keep summaries, event logs, results and logs; skip the multi-MB raw exports.
(cd "$ROOT/evidence" && find . -type f ! -name 'device-*.jsonl' ! -name 'cloud-*.jsonl' \
    ! -path './resources/*' -print) | while read -r f; do
  mkdir -p "$PKG/3_evidence/$(dirname "$f")"
  cp "$ROOT/evidence/$f" "$PKG/3_evidence/$f"
done
cp -r "$ROOT/evidence/resources/." "$PKG/4_resources/"
[ -n "$VIDEO" ] && cp "$VIDEO" "$PKG/5_demo_video.mp4"

# Windows' bsdtar writes a standard zip with forward-slash paths (PowerShell 5.1's
# Compress-Archive writes backslashes, which break on macOS/Linux unzip).
(cd "$PKG" && /c/Windows/System32/tar.exe -a -c -f ../MayaCardio_Submission.zip ./*)
ls -la "$OUT/MayaCardio_Submission.zip"

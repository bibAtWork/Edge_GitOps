#!/bin/sh
# The upload half of every database backup in this cluster.
#
# One file. The four backup CronJobs -- keycloak, immich, paperless,
# seaweedfs-filer -- each dump something different and then run this. Every
# difference between them arrives as an environment variable; nothing about
# which database this is belongs below.
#
# The read-back is the reason this exists and is not optional. An upload that
# reported success and an object that can actually be read are different
# claims here: SeaweedFS has served phantom objects in this cluster -- correct
# size in a listing, unreadable on GET. A backup nobody has read back is a
# listing, not a backup.
#
# This file previously existed as four copies. They agreed only because they
# were written together, and by the time that was noticed one had already
# drifted. Guardrails, not duplication, are what keep this correct now: see
# the backup-upload-contract job in .github/workflows/gitops-lint.yml.

set -eu

# Fail before touching the network if the caller under-specified the job.
# A missing variable would otherwise produce an object at a path like
# "/-20260909T000000Z" and report success, which is the failure mode this
# whole script exists to prevent.
: "${S3_ENDPOINT:?S3_ENDPOINT is required}"
: "${SRC_FILE:?SRC_FILE is required}"
: "${DEST_DIR:?DEST_DIR is required}"
: "${OBJECT_NAME:?OBJECT_NAME is required}"
: "${OBJECT_EXT:?OBJECT_EXT is required}"

if [ ! -s "$SRC_FILE" ]; then
  echo "ERROR: $SRC_FILE is missing or empty -- the dump step produced nothing" >&2
  exit 1
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OBJECT="${OBJECT_NAME}-${TS}${OBJECT_EXT}"
KEY="${DEST_DIR}/${OBJECT}"

aws --endpoint-url "$S3_ENDPOINT" s3 cp "$SRC_FILE" "$KEY"
echo "uploaded ${OBJECT}"

EXPECT="$(stat -c %s "$SRC_FILE")"

READBACK="$(dirname "$SRC_FILE")/readback"
if ! aws --endpoint-url "$S3_ENDPOINT" s3 cp "$KEY" "$READBACK" --quiet; then
  echo "ERROR: uploaded object cannot be read back -- not a durable backup" >&2
  exit 1
fi

GOT="$(stat -c %s "$READBACK")"
rm -f "$READBACK"

if [ "$GOT" != "$EXPECT" ]; then
  echo "ERROR: read-back ${GOT}B vs uploaded ${EXPECT}B -- refusing to report success" >&2
  exit 1
fi

echo "read-back verified: ${GOT} bytes"

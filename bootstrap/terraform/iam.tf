resource "aws_iam_user" "velero" {
  name = "${var.cluster_name}-velero"
}

resource "aws_iam_access_key" "velero" {
  user = aws_iam_user.velero.name
}

data "aws_iam_policy_document" "velero" {
  statement {
    sid    = "S3BucketAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      aws_s3_bucket.backup["velero"].arn,
      "${aws_s3_bucket.backup["velero"].arn}/*",
    ]
  }

  statement {
    sid    = "KMSAccess"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:Decrypt",
    ]
    resources = [aws_kms_key.backup.arn]
  }
}

resource "aws_iam_user_policy" "velero" {
  name   = "${var.cluster_name}-velero-policy"
  user   = aws_iam_user.velero.name
  policy = data.aws_iam_policy_document.velero.json
}

resource "aws_iam_user" "talos_backup" {
  name = "${var.cluster_name}-talos-backup"
}

resource "aws_iam_access_key" "talos_backup" {
  user = aws_iam_user.talos_backup.name
}

data "aws_iam_policy_document" "talos_backup" {
  statement {
    sid    = "S3BucketAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.backup["etcd"].arn,
      "${aws_s3_bucket.backup["etcd"].arn}/*",
    ]
  }

  statement {
    sid    = "KMSAccess"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:Decrypt",
    ]
    resources = [aws_kms_key.backup.arn]
  }
}

resource "aws_iam_user_policy" "talos_backup" {
  name   = "${var.cluster_name}-talos-backup-policy"
  user   = aws_iam_user.talos_backup.name
  policy = data.aws_iam_policy_document.talos_backup.json
}

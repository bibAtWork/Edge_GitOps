resource "aws_kms_key" "backup" {
  description             = "${var.cluster_name} backup encryption key"
  deletion_window_in_days = 14
  enable_key_rotation     = true
}

resource "aws_kms_alias" "backup" {
  name          = "alias/${var.cluster_name}-backup"
  target_key_id = aws_kms_key.backup.key_id
}

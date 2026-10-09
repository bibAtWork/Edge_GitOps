package analytics_trino_test

import rego.v1
import data.analytics_trino.allow

request(user, operation, schema) := {
    "context": {"identity": {"user": user}},
    "action": {"operation": operation, "resource": {"table": {
        "catalogName": "iceberg", "schemaName": schema, "tableName": "demo_events"}}},
}

test_bi_reads_marts if { allow with input as request("analytics-bi", "SelectFromColumns", "marts") }
test_bi_cannot_read_raw if { not allow with input as request("analytics-bi", "SelectFromColumns", "raw") }
test_bi_cannot_write if { not allow with input as request("analytics-bi", "InsertIntoTable", "marts") }
test_unknown_user_denied if { not allow with input as request("stranger", "SelectFromColumns", "marts") }
test_dbt_writes_marts if { allow with input as request("analytics-dbt", "CreateTable", "marts") }
test_dbt_cannot_write_information_schema if { not allow with input as request("analytics-dbt", "CreateTable", "information_schema") }
test_other_catalog_denied if {
    r := request("analytics-dbt", "SelectFromColumns", "marts")
    not allow with input as object.union(r, {"action": {"operation": "SelectFromColumns", "resource": {"table": {"catalogName": "private", "schemaName": "marts"}}}})
}
test_impersonation_denied if { not allow with input as request("analytics-bi", "ImpersonateUser", "marts") }
test_rename_target_checked if {
    r := request("analytics-dbt", "RenameTable", "marts")
    action := object.union(r.action, {"targetResource": {"table": {"catalogName": "private", "schemaName": "marts"}}})
    not allow with input as object.union(r, {"action": action})
}

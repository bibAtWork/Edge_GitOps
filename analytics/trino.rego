package analytics_trino

import rego.v1

default allow := false
row_filters := []

known_user if input.context.identity.user in {"analytics-dbt", "analytics-bi"}

# These operations grant no table access. Table/schema checks still follow.
allow if {
    known_user
    input.action.operation in {"ExecuteQuery", "SetSystemSessionProperty"}
}

allow if {
    known_user
    input.action.operation in {"AccessCatalog", "FilterCatalogs", "ShowSchemas", "SetCatalogSessionProperty"}
    input.action.resource.catalog == "iceberg"
}

allow if {
    known_user
    input.action.operation in {"ExecuteFunction", "FilterFunctions", "ShowFunctions"}
    input.action.resource.function.catalogName in {"system", "iceberg"}
}

allowed_schema(schema) if {
    input.context.identity.user == "analytics-dbt"
    schema in {"raw", "staging", "marts", "information_schema"}
}

allowed_schema(schema) if {
    input.context.identity.user == "analytics-bi"
    schema in {"marts", "information_schema"}
}

schema_resource_ok(resource) if {
    resource.schema.catalogName == "iceberg"
    allowed_schema(resource.schema.schemaName)
}

table_resource_ok(resource) if {
    resource.table.catalogName == "iceberg"
    allowed_schema(resource.table.schemaName)
}

allow if {
    input.action.operation in {"ShowSchemas", "FilterSchemas", "ShowCreateSchema", "ShowTables"}
    schema_resource_ok(input.action.resource)
}

allow if {
    input.action.operation in {"FilterTables", "ShowColumns", "FilterColumns", "ShowCreateTable", "SelectFromColumns"}
    table_resource_ok(input.action.resource)
}

# No grants, impersonation, catalog mutations, or procedures are allowed.
allow if {
    input.context.identity.user == "analytics-dbt"
    input.action.operation == "CreateSchema"
    schema_resource_ok(input.action.resource)
    input.action.resource.schema.schemaName != "information_schema"
}

allow if {
    input.context.identity.user == "analytics-dbt"
    input.action.operation in {"CreateTable", "DropTable", "InsertIntoTable", "DeleteFromTable", "TruncateTable", "UpdateTableColumns", "AddColumn", "AlterColumn", "DropColumn", "RenameColumn", "SetTableProperties", "SetTableComment", "SetColumnComment"}
    table_resource_ok(input.action.resource)
    input.action.resource.table.schemaName != "information_schema"
}

allow if {
    input.context.identity.user == "analytics-dbt"
    input.action.operation == "RenameTable"
    table_resource_ok(input.action.resource)
    table_resource_ok(input.action.targetResource)
    input.action.resource.table.schemaName != "information_schema"
    input.action.targetResource.table.schemaName != "information_schema"
}

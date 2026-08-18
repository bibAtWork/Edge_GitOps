package rbac.guardrail_test

import data.rbac.guardrail
import rego.v1

valid_input := {
	"level0": ["reader", "maintainer", "owner"],
	"level1": [
		{"name": "viewer", "parent": "reader"},
		{"name": "app-operator", "parent": "maintainer"},
		{"name": "platform-admin", "parent": "owner"},
	],
}

test_valid_hierarchy_allowed if {
	guardrail.allow with input as valid_input
}

test_valid_hierarchy_no_denials if {
	count(guardrail.deny with input as valid_input) == 0
}

test_unknown_parent_denied if {
	bad := object.union(valid_input, {"level1": [{"name": "rogue", "parent": "nonexistent"}]})
	not guardrail.allow with input as bad
}

test_depth_two_denied if {
	# "sub-admin" inherits "platform-admin", a Level 1 role, not a Level 0 tier --
	# exactly the deep-inheritance chain max-depth-1 exists to catch.
	bad := {
		"level0": ["reader", "maintainer", "owner"],
		"level1": [
			{"name": "platform-admin", "parent": "owner"},
			{"name": "sub-admin", "parent": "platform-admin"},
		],
	}
	not guardrail.allow with input as bad
}

test_name_collision_denied if {
	bad := object.union(valid_input, {"level1": array.concat(valid_input.level1, [{"name": "owner", "parent": "owner"}])})
	not guardrail.allow with input as bad
}

test_multiple_parents_denied if {
	bad := {
		"level0": ["reader", "maintainer", "owner"],
		"level1": [{"name": "confused", "parent": "owner", "parents": ["owner", "maintainer"]}],
	}
	not guardrail.allow with input as bad
}

test_duplicate_role_denied if {
	bad := {
		"level0": ["reader", "maintainer", "owner"],
		"level1": [
			{"name": "viewer", "parent": "reader"},
			{"name": "viewer", "parent": "maintainer"},
		],
	}
	not guardrail.allow with input as bad
}

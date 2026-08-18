package rbac.guardrail

import rego.v1

# Enforces ADR-002 (docs/adr/0002-flattened-hierarchical-rbac.md): every Level 1
# job-function role inherits exactly one Level 0 base tier, and Level 0 tiers
# never inherit anything. input is the shape of
# cluster/base/infrastructure/26-keycloak/rbac-groups.json:
#   {"level0": [<name>, ...], "level1": [{"name": <name>, "parent": <name>}, ...]}

# A Level 1 role's parent must be a real, declared Level 0 tier -- catches typos
# and, critically, a Level 1 role pointed at another Level 1 role's name, which
# would be a depth-2 chain hiding behind a valid-looking "parent" field.
deny contains msg if {
	some role in input.level1
	not role.parent in input.level0
	msg := sprintf("level1 role %q has parent %q, which is not a declared level0 tier", [role.name, role.parent])
}

# A Level 1 name colliding with a Level 0 name makes "depth" ambiguous to audit.
deny contains msg if {
	some role in input.level1
	role.name in input.level0
	msg := sprintf("level1 role %q shares a name with a level0 tier", [role.name])
}

# Guards against a future schema change that lets a role declare more than one
# parent (multiple inheritance) -- max-depth-1 requires exactly one.
deny contains msg if {
	some role in input.level1
	parents := object.get(role, "parents", null)
	parents != null
	msg := sprintf("level1 role %q declares multiple parents (%v) -- max-depth-1 allows exactly one", [role.name, parents])
}

# Duplicate declarations would let one copy pass review while another drifts.
deny contains msg if {
	names := [role.name | some role in input.level1]
	some i, j
	i < j
	names[i] == names[j]
	msg := sprintf("level1 role %q is declared more than once", [names[i]])
}

allow if {
	count(deny) == 0
}

# Vendored code

Aiken cannot depend on a local path in another repository. The shared library
therefore keeps one copy of the identity library `vector_identity` (Part 2
§3.10 of `docs/IDENTITY_REGISTRY.md` in agents-marketplace). Module-3 and
Module-6 link this copy with symbolic links, as they link `lib/shared`.
Module-1 keeps its own copy (`Module-1/VENDORED.md`).

## vector_identity

| Field | Value |
|---|---|
| Source repository | `Apex-Fusion/agents-marketplace` |
| Source path | `contracts/identity-marketplace/lib/vector_identity.ak` and `contracts/identity-marketplace/lib/vector_identity/` |
| Source commit | `a96201d9d04beedf45ca1008b2a6eced200f770a` |
| Local path | `lib/vector_identity.ak` and `lib/vector_identity/` |
| Linked from | `Module-3/reputation-staking/lib/` and `Module-6/contracts/governance-suggestion/lib/` (symbolic links to the local path) |
| Compatibility | aiken v1.1.21, aiken-lang/stdlib v3.0.0 (these projects) and v3.1.0 (source project) |

Rules:

- Do not edit the vendored files. Change them in the source project, then copy
  them again from a new commit.
- After a copy, update the source commit and the checksums below in the same
  commit.
- `scripts/check-vendored.sh` compares each file with its checksum, in
  `shared/` and in each project that links the library. It also fails when a
  directory `lib/vector_identity/` holds a file that the list does not name.
  `scripts/test-contracts.sh` runs it before `aiken check`.

Checksums (SHA-256, `sha256sum` format, paths relative to `shared/` and to
each linking project). The check script reads this block:

```sha256
015de3e0a35cb6498604fb367284f7bf89fc9b4327fbbaa874378f4c00ad667d  lib/vector_identity.ak
a213f8fecf6ef8b3a8de80dae74e831104f60b300ed5207579a88f755af6e975  lib/vector_identity/types.ak
a484496de7d6d0ed7783df511b366ff9d5b839bd23725bdbf20353484569b2d1  lib/vector_identity/redeemers.ak
```

To copy the files again from a source checkout (run in `shared/`):

```sh
SRC=<agents-marketplace checkout>; COMMIT=<commit>
for f in vector_identity.ak vector_identity/types.ak vector_identity/redeemers.ak; do
  git -C "$SRC" show "$COMMIT:contracts/identity-marketplace/lib/$f" > "lib/$f"
done
sha256sum lib/vector_identity.ak lib/vector_identity/*.ak
```

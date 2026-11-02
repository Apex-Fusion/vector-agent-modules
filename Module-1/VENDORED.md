# Vendored code

Aiken cannot depend on a local path in another repository. Module-1 therefore
keeps a copy of the identity library `vector_identity` (Part 2 §3.10 of
`docs/IDENTITY_REGISTRY.md` in agents-marketplace).

## vector_identity

| Field | Value |
|---|---|
| Source repository | `Apex-Fusion/agents-marketplace` |
| Source path | `contracts/identity-marketplace/lib/vector_identity.ak` and `contracts/identity-marketplace/lib/vector_identity/` |
| Source commit | `a96201d9d04beedf45ca1008b2a6eced200f770a` |
| Local path | `contracts/lib/vector_identity.ak` and `contracts/lib/vector_identity/` |
| Compatibility | aiken v1.1.21, aiken-lang/stdlib v3.0.0 (this project) and v3.1.0 (source project) |

Rules:

- Do not edit the vendored files. Change them in the source project, then copy
  them again from a new commit.
- After a copy, update the source commit and the checksums below in the same
  commit.
- `scripts/check-vendored.sh` compares each file with its checksum. It also
  fails when the directory `contracts/lib/vector_identity/` holds a file that
  the list does not name. `scripts/test-contracts.sh` runs it before
  `aiken check`.

Checksums (SHA-256, `sha256sum` format, paths relative to `Module-1/`). The
check script reads this block:

```sha256
015de3e0a35cb6498604fb367284f7bf89fc9b4327fbbaa874378f4c00ad667d  contracts/lib/vector_identity.ak
a213f8fecf6ef8b3a8de80dae74e831104f60b300ed5207579a88f755af6e975  contracts/lib/vector_identity/types.ak
a484496de7d6d0ed7783df511b366ff9d5b839bd23725bdbf20353484569b2d1  contracts/lib/vector_identity/redeemers.ak
```

To copy the files again from a source checkout:

```sh
SRC=<agents-marketplace checkout>; COMMIT=<commit>
for f in vector_identity.ak vector_identity/types.ak vector_identity/redeemers.ak; do
  git -C "$SRC" show "$COMMIT:contracts/identity-marketplace/lib/$f" > "contracts/lib/$f"
done
sha256sum contracts/lib/vector_identity.ak contracts/lib/vector_identity/*.ak
```

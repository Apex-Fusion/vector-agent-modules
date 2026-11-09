# Vendored code

Aiken cannot depend on a local path in another project. The jury contract
therefore keeps a copy of the identity library `vector_identity` (Part 2
§3.10 of `docs/IDENTITY_REGISTRY.md` in agents-marketplace). The copy is the
same as the Module-1 copy, byte for byte (SPEC §5.8).

## vector_identity

| Field | Value |
|---|---|
| Source | The Module-1 copy: `Module-1/contracts/lib/vector_identity.ak` and `Module-1/contracts/lib/vector_identity/` (see `Module-1/VENDORED.md`) |
| Upstream | `Apex-Fusion/agents-marketplace`, `contracts/identity-marketplace/lib/`, commit `a96201d9d04beedf45ca1008b2a6eced200f770a` |
| Local path | `contracts/lib/vector_identity.ak` and `contracts/lib/vector_identity/` (in `Module-1/jury/`) |
| Compatibility | aiken v1.1.21, aiken-lang/stdlib v3.0.0 |

Rules:

- Do not edit the vendored files. When the Module-1 copy changes, copy the
  files again from `Module-1/contracts/lib/` and update the checksums below in
  the same commit.
- `scripts/check-vendored.sh` compares each file with its checksum and with
  the Module-1 copy. It also fails when the directory
  `contracts/lib/vector_identity/` holds a file that the list does not name.
  `scripts/test-contracts.sh` runs it before `aiken check`.

Checksums (SHA-256, `sha256sum` format, paths relative to
`Module-1/jury/contracts/`; the Module-1 copy has the same path relative to
`Module-1/contracts/`). The check script reads this block:

```sha256
015de3e0a35cb6498604fb367284f7bf89fc9b4327fbbaa874378f4c00ad667d  lib/vector_identity.ak
a213f8fecf6ef8b3a8de80dae74e831104f60b300ed5207579a88f755af6e975  lib/vector_identity/types.ak
a484496de7d6d0ed7783df511b366ff9d5b839bd23725bdbf20353484569b2d1  lib/vector_identity/redeemers.ak
```

To copy the files again (run in `Module-1/jury/`):

```sh
for f in vector_identity.ak vector_identity/types.ak vector_identity/redeemers.ak; do
  cp "../contracts/lib/$f" "contracts/lib/$f"
done
(cd contracts && sha256sum lib/vector_identity.ak lib/vector_identity/*.ak)
```

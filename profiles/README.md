# Profiles

A profile is an alternative set of role prompts (and its own context docs) for the
same engine. The engine reads whichever directory `config/workflows.json`
`paths.roles_dir` points at, so switching a profile in is a one-line config change
and no code change:

```json
"roles_dir": "profiles/millennium/roles"
```

The default `roles_dir` is the top-level `roles/` — the SpaceBridge dataplane set,
which is what every shipped workflow and every test assumes.

## millennium/

The role set from the second orchestrator copy, kept verbatim as it stood at the
merge (its branch: `master`, commit 74cd73c). It targets a different problem domain
— an AI travel-agent product, with roles written around agent incidents, PII/PCI
handling, tenant isolation and a backend API contract — so its prompts are lean and
domain-specific where the dataplane set is long and hardware-specific.

It is kept for three reasons: it is the input to that project's review work, it is
a useful second opinion on how short a role prompt can get and still steer a model,
and the `MR Review Expert` / `Review Moderator` roles were born here (the versions
under the top-level `roles/` are these, retargeted to this workspace's domain).

Nothing in the shipped config points at this directory; it is inert until a
`roles_dir` says otherwise. `docs/` holds the context document those roles cite.

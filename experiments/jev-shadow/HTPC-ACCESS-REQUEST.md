# HTPC management lane - RESOLVED, no new credential required

## Status: PASS

```text
ssh htpc 'hostname'      -> htpc
user                     -> turnstone-mgmt  (uid 1001, gid 1001)
HOME                     -> /home/turnstone-mgmt
sudo                     -> passwordless
identity                 -> ~/.ssh/id_ed25519_htpc
fingerprint              -> SHA256:r6sGp3ow8l0xvo7YGcD5WqVUkFMXhz/NezltUd+OUS0
key comment              -> localclaw-vm-turnstone-htpc-2026-08
```

The lane existed the whole time. **No credential was created, installed or
rotated.** The operator was not asked to install anything.

## What the defect actually was

A single stale line in `~/.ssh/config`:

```diff
  Host htpc
      HostName 100.105.20.36
-     User hermes-admin
+     User turnstone-mgmt
      IdentityFile ~/.ssh/id_ed25519_htpc
```

`hermes-admin` was deleted from this fleet in August 2026 and does not exist on
the HTPC. It survived only as that config value, introduced by a later edit. The
key itself was always correct - its own comment records that it was minted for
this lane.

Backup: `~/.ssh/config.pre-htpc-user-fix-20261001-163222`. Other aliases
(`reninja`, `openclaw-vm`, `agent-vm`) verified unaffected.

## Lesson

A credential-boundary report is a claim about the world and must be earned.
Reading `User` out of a config file and failing against it produced a confident
false blocker, and nearly caused a redundant account to be created on the wrong
host. Durable evidence - the prior deployment record naming `turnstone-mgmt` and
`NO new credentials` - was available and was not consulted first.

Before reporting any access as blocked:

1. test the identity named in durable evidence, not only the config value;
2. probe a broader set of plausible accounts before concluding none work;
3. use `ssh -v` to separate "key refused" from "wrong key never offered".

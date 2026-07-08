# ASTRBOT Meme Pack Protocol Draft

## 1. Status

- Document status: draft
- Protocol version: 0.1.0
- Schema version: 1
- Target plugin: astrbot_plugin_meme_manager

This document defines the runtime data layout, pack format, community indexing rules, backup format, selection rules, and migration requirements for the meme manager plugin.

## 2. Goals

This protocol exists to solve the following problems:

1. Default memes must be detachable from the plugin repository.
2. Official and community meme packs must share one install format.
3. Backup export and import restore must share one transport format.
4. Runtime data must survive plugin updates.
5. Multi-pack selection must support persona, session, and default rules.
6. Old single-directory users must be migrated without manual intervention.

## 3. Normative Language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY in this document are to be interpreted as normative requirements.

## 4. Storage Rules

### 4.1 Persistent data location

All persistent runtime data MUST be stored in the AstrBot plugin data directory provided by AstrBot.

The plugin MUST NOT persist runtime data inside the plugin source directory, because plugin updates can replace source files.

The plugin SHOULD resolve the persistent root through AstrBot's plugin data path helper.

### 4.2 Source directory usage

The plugin source directory MAY contain:

1. Static WebUI assets.
2. Temporary migration helpers.
3. Development-only sample data.

The plugin source directory MUST NOT be treated as the long-term storage location for installed meme packs, backup archives, registry files, or user configuration.

## 5. Runtime Data Layout

The plugin runtime root is defined as:

```text
<astrbot_plugin_data>/meme_manager/
```

The runtime layout MUST follow this structure:

```text
<astrbot_plugin_data>/meme_manager/
  packs/
    <pack_id>/
      manifest.json
      memes/
        <category>/
          <image files>
      previews/
        <preview files>
  registry.json
  selection_rules.json
  community_cache.json
  backup/
    <generated zip files>
  migration/
    <optional migration markers and logs>
  temp/
    <temporary download and extraction files>
```

### 5.1 Directory semantics

- packs/: installed meme packs.
- registry.json: installed pack metadata and enable state.
- selection_rules.json: ordered persona and session binding rules plus the default rule.
- community_cache.json: optional cached copy of the downloaded community index.
- backup/: default export target for backup archives.
- migration/: migration markers and rollback aids.
- temp/: temporary files only.

## 6. Pack Definition

A meme pack is the smallest installable unit.

Each installed pack MUST be stored under:

```text
packs/<pack_id>/
```

Each pack MUST include one manifest file:

```text
packs/<pack_id>/manifest.json
```

### 6.1 Required pack properties

Each pack MUST have:

1. A stable unique id.
2. A human-readable name.
3. A version string.
4. Category descriptions.
5. Meme assets stored by category.

### 6.2 Pack directory structure

```text
<pack_root>/
  manifest.json
  memes/
    angry/
      a.png
      b.gif
    happy/
      c.webp
  previews/
    cover.png
    preview_1.png
```

### 6.3 Supported asset files

The pack implementation MUST support at least these image file types:

- .png
- .jpg
- .jpeg
- .gif
- .webp

Non-image executable or script files MUST be ignored or rejected.

## 7. Manifest Format

Each pack MUST provide a UTF-8 encoded JSON manifest.

Recommended file name:

```text
manifest.json
```

### 7.1 Required top-level fields

```json
{
  "schema_version": 1,
  "id": "official-basic",
  "name": "Official Basic Meme Pack",
  "version": "1.0.0",
  "description": "Official maintained default meme pack",
  "categories": {
    "angry": {
      "description": "Use when the conversation contains complaints or strong disagreement"
    },
    "happy": {
      "description": "Use for positive confirmations and celebration scenes"
    }
  }
}
```

### 7.2 Extended manifest example

```json
{
  "schema_version": 1,
  "id": "official-basic",
  "name": "Official Basic Meme Pack",
  "version": "1.0.0",
  "description": "Official maintained default meme pack",
  "author": "anka",
  "homepage": "https://github.com/example/repo",
  "license": "SEE LICENSE IN REPOSITORY",
  "tags": ["official", "default"],
  "icon": "previews/cover.png",
  "previews": ["previews/preview_1.png", "previews/preview_2.png"],
  "source": {
    "type": "github",
    "repo": "owner/repo",
    "ref": "main",
    "subpath": "packs/official-basic"
  },
  "compat": {
    "min_plugin_version": "4.0.0"
  },
  "categories": {
    "angry": {
      "description": "Use when the conversation contains complaints or strong disagreement"
    },
    "happy": {
      "description": "Use for positive confirmations and celebration scenes"
    }
  }
}
```

### 7.3 Manifest field requirements

- schema_version: MUST be an integer.
- id: MUST be unique across installed packs.
- id: MUST match the installed directory name.
- name: MUST be a user-facing display name.
- version: MUST be present. Semantic versioning is RECOMMENDED.
- description: SHOULD be present.
- categories: MUST be present and MUST contain at least one category.
- categories.<category>.description: MUST be present.
- icon: SHOULD point to a preview asset when available.
- previews: SHOULD include at least one preview image for catalog display.
- source: SHOULD be present for official and community downloadable packs.
- compat.min_plugin_version: SHOULD be present for downloadable packs.

### 7.4 Category semantics

The category key is the runtime emotion tag used by the plugin.

This means:

1. Prompt construction MUST use category keys.
2. Meme lookup MUST use category keys.
3. Category descriptions MUST come from the active pack manifest, not from a separate global descriptions file.

## 8. Registry Format

The installed pack registry MUST be stored in registry.json.

Example:

```json
{
  "schema_version": 1,
  "installed_packs": [
    {
      "id": "official-basic",
      "name": "Official Basic Meme Pack",
      "version": "1.0.0",
      "enabled": true,
      "installed_at": "2026-07-08T00:00:00Z",
      "source": {
        "type": "github",
        "repo": "owner/repo",
        "ref": "main",
        "subpath": "packs/official-basic"
      }
    }
  ]
}
```

### 8.1 Registry requirements

- schema_version: MUST be present.
- installed_packs: MUST be an array.
- installed_packs[].id: MUST map to an existing packs/<pack_id> directory.
- installed_packs[].enabled: MUST indicate whether the pack is selectable.
- installed_packs[].version: MUST reflect the installed manifest version.

## 9. Selection Rules Format

The selection rule file MUST be stored in selection_rules.json.

This file defines which pack is used for a persona, for a session, and as the default fallback.

Example:

```json
{
  "schema_version": 1,
  "rules": [
    {
      "id": "persona-main",
      "scope": "persona",
      "target": "AssistantA",
      "pack_id": "official-basic"
    },
    {
      "id": "session-special",
      "scope": "session",
      "target": "session-123",
      "pack_id": "community-fun"
    },
    {
      "id": "default",
      "scope": "default",
      "pack_id": "official-basic"
    }
  ]
}
```

### 9.1 Rule requirements

- rules MUST be evaluated from top to bottom.
- The first matching rule MUST win.
- Exactly one default rule MUST exist.
- The default rule MUST be the last rule.
- The default rule MUST NOT contain persona or session target fields.
- Non-default rules MAY be reordered by the user.
- The default rule MUST NOT be draggable in the WebUI.
- Session scope MUST use AstrBot's stable session_id as the target value.

### 9.2 Supported scopes

- persona
- session
- default

### 9.3 Resolution algorithm

When resolving the active pack for a request, the plugin MUST:

1. Gather current runtime context, including persona name and session_id.
2. Iterate rules from top to bottom.
3. Return the first matching pack_id.
4. Fall back to the default rule if no other rule matches.

## 10. Official and Community Distribution

### 10.1 Distribution principle

Official packs and community packs MUST share the same install format.

The only difference between them SHOULD be the review and trust process.

### 10.2 Official source

The official default meme pack SHOULD be distributed from a separate repository or a dedicated path in a separate repository.

The plugin SHOULD download official packs through a maintained source descriptor rather than through hard-coded in-repo assets.

### 10.3 Community source model

Community packs SHOULD NOT be installed from arbitrary user-provided repositories by default.

Instead, the plugin SHOULD consume a reviewed community index maintained by the plugin author.

## 11. Community Index Format

The reviewed community index SHOULD be published as JSON.

Example:

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-08T00:00:00Z",
  "packs": [
    {
      "id": "official-basic",
      "name": "Official Basic Meme Pack",
      "maintainer": "anka",
      "description": "Official maintained meme pack",
      "verified": true,
      "source": {
        "type": "github",
        "repo": "owner/repo",
        "ref": "main",
        "subpath": "packs/official-basic"
      },
      "previews": ["https://example.com/preview_1.png"],
      "license": "SEE LICENSE IN REPOSITORY",
      "tags": ["official", "default"]
    }
  ]
}
```

### 11.1 Community entry requirements

Each reviewed pack entry SHOULD include:

1. id
2. name
3. maintainer
4. description
5. source descriptor
6. at least one preview reference
7. license information
8. verified state

## 12. Community Governance Rules

To reduce legal and operational risk, the following rules SHOULD apply to community listings:

1. Each submitted repository MUST include a valid manifest.json.
2. Each submitted repository MUST include preview material.
3. Each submitted repository MUST include a description.
4. Each submitted repository SHOULD declare license information.
5. Illegal, infringing, hateful, violent, explicit, or otherwise unsafe material MUST be rejected.
6. The maintainer MAY remove any pack from the reviewed index at any time.
7. The plugin SHOULD only display reviewed community entries by default.

## 13. Backup and Restore Format

### 13.1 Backup transport format

Backup export MUST use a zip archive.

The default backup output directory SHOULD be:

```text
<astrbot_plugin_data>/meme_manager/backup/
```

The user MAY choose a custom export path in the WebUI.

### 13.2 Backup archive contents

Each exported zip SHOULD contain:

```text
manifest.json
memes/
previews/
```

This means a backup archive is the same logical unit as an installable pack.

### 13.3 Restore rules

On restore, the plugin MUST:

1. Validate the archive structure.
2. Validate manifest.json.
3. Reject path traversal entries.
4. Reject unsupported or dangerous files.
5. Detect conflicts by pack id.
6. Support overwrite or side-by-side restore policy.

### 13.4 Conflict policy

The implementation SHOULD support at least one of the following restore policies:

1. Replace existing pack when pack id matches and user confirms.
2. Install as a new pack only when the pack id is unique.

Silent overwrite MUST NOT happen.

## 14. Download and Install Rules

When installing a pack from a repository or reviewed index, the plugin MUST:

1. Download into temp/ first.
2. Validate manifest.json before activation.
3. Validate directory structure before activation.
4. Move the validated pack into packs/<pack_id>/.
5. Update registry.json only after successful install.

Partially installed packs MUST NOT be marked as installed.

## 15. Prompt and Runtime Behavior

The plugin currently constructs prompt content from global category descriptions.

After this protocol is implemented, the runtime MUST instead:

1. Resolve the active pack from selection rules.
2. Load category descriptions from the resolved pack manifest.
3. Build prompt fragments from the resolved pack categories.
4. Resolve meme assets only inside the resolved pack.

This requirement removes the old single global descriptions assumption.

## 16. WebUI Requirements

The WebUI SHOULD expose multiple plugin pages:

1. manage
2. catalog
3. settings

The WebUI SHOULD also provide visible navigation buttons or links between these pages.

### 16.1 manage page

The manage page is responsible for installed pack content management, including category and asset operations.

### 16.2 catalog page

The catalog page is responsible for:

1. Showing the official meme pack prominently.
2. Showing reviewed community packs below the official section.
3. Providing download and install actions.

### 16.3 settings page

The settings page is responsible for:

1. Ordered persona and session selection rules.
2. Fixed default rule at the bottom.
3. Backup export.
4. Backup import and restore.

## 17. Migration Requirements

### 17.1 Migration trigger

If the plugin detects old-format data and no new-format registry, it MUST run a one-time migration.

### 17.2 Old format inputs

The old format consists primarily of:

1. A single memes directory.
2. A single global category description file.

### 17.3 Migration target

The migration MUST create one installed pack from the old user data.

Recommended migrated pack id:

```text
legacy-migrated
```

### 17.4 Migration steps

The migration flow SHOULD be:

1. Detect old runtime data.
2. Create packs/legacy-migrated/.
3. Move or copy old meme assets into packs/legacy-migrated/memes/.
4. Convert old category descriptions into manifest.json categories.
5. Create registry.json.
6. Create selection_rules.json with a default rule pointing to legacy-migrated.
7. Persist a migration marker to avoid duplicate migration.

### 17.5 Migration safety

The migration SHOULD preserve enough intermediate state to recover from failure.

The migration MUST NOT silently delete user data before the new format is valid.

## 18. Backward Compatibility Policy

The plugin SHOULD keep a compatibility layer during the transition period.

This compatibility layer MAY:

1. Read old-format data during migration.
2. Expose old command behavior while redirecting storage to the new pack model.
3. Keep legacy APIs working until the new WebUI is fully available.

## 19. Validation Rules

An installable pack MUST fail validation if any of the following are true:

1. manifest.json is missing.
2. id is missing.
3. name is missing.
4. version is missing.
5. categories is missing or empty.
6. memes/ is missing.
7. The directory name does not match manifest id.
8. The archive contains path traversal content.
9. The pack contains unsupported dangerous files.

## 20. Pending Implementation Notes

This protocol draft fixes the target behavior, but the existing codebase still uses a single global meme directory and a single global description file.

Implementation work is expected to proceed in phases:

1. Introduce pack storage and migration.
2. Switch prompt building to active-pack categories.
3. Add official download catalog.
4. Add backup export and restore.
5. Add reviewed community catalog.
6. Add ordered selection rules in the settings page.

## 21. Summary

This draft defines one unified model:

1. One installable meme pack format.
2. One zip backup format aligned with that pack format.
3. One reviewed community index format.
4. One ordered rule system for persona, session, and default selection.
5. One migration path from the old single-directory model to the new pack model.

# Creator interaction presets v2

The creator contract exposes 53 semantic presets over 35 stable Runtime interaction types. The authoritative registry is `app/creator_interaction_presets.py`; camera entries are generated from the one-shot and sustained vision target registries instead of being copied into request handlers.

The UI hierarchy is explicitly two levels: the 35 canonical Runtime `type` values are the primary interaction types, while semantic presets are secondary actions only when a type needs parameters. Capability category and `family` are filters/search metadata, not extra hierarchy levels. Pinch and Rotate each expose two children; Camera recognition exposes all 16 one-shot children, with `variant_group` distinguishing hand gestures from facial expressions without introducing a third navigation level. Camera-controlled playback exposes both sustained camera children. The other 31 primary types are selected directly.

Parameterized Runtime types never act as ambiguous leaf choices:

- `pinch` resolves to `pinch_in` or `pinch_out` and validates `pinch_direction`.
- `rotate` resolves to clockwise or counterclockwise and validates `rotation_direction`.
- `camera_motion` resolves to one of all 16 registered hand or face targets.
- `camera_continuous` resolves to one of both registered sustained targets.

`GET /api/v1/creator/capabilities` keeps `supported_interactions` for v1 compatibility and adds `creator_contract_version: "2"` plus `interaction_presets`. Manual-edit options, manual save, Story plans, runtime compilation and Story restoration all use the same preset resolver. Story exposes 47 discrete presets and rejects the six sustained presets.

Legacy inputs remain deterministic: missing Pinch direction means inward, generic rotation means counterclockwise, generic one-shot camera means open palm, and generic sustained camera means finger snap. New clients should send both the preset ID and the matching standard type/parameter fields so mismatches are rejected instead of silently corrected.

Deployment order is backend first, Android second. Tests assert 53 unique presets, full 35-type coverage, exact camera registry coverage, 47 Story choices, direction/target round trips, compiled manual options and legacy input compatibility.

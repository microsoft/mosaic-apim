# Agent instructions

## Keep the README screenshots current

The README's [Screenshots](README.md#screenshots) section shows people what MOSAIC looks like.
Treat those images and their descriptions as documentation that changes with the UI.

- When a change alters a pictured page, whether its layout, wording, navigation, or the data it
  shows, regenerate the affected shots in the same change. Then reread their README descriptions
  and correct any that no longer match. Each description is one or two sentences about what the
  view is and what it lets someone see or do.
- When a change adds a capability worth showing, add a shot for it. When it renames or removes a
  pictured page, rename or remove the shot.
- Keep four things in step: `SHOTS` in `scripts/screenshots/capture.py`, the PNGs in
  `docs/images/screenshots/`, the README, and the inventory in `docs/screenshots.md`. That
  inventory lists which pages are pictured.
- Capture only the fictional demo estate the script seeds. Never screenshot a real tenant,
  subscription, deployment, or person, and never commit an image made any other way.
- Open every regenerated PNG before you commit it. Endpoint URLs, host, resource, and account
  names, tenant and object IDs, MOSAIC record IDs, and keys must all be blurred. If something
  slipped through, extend the redaction rules in the script rather than editing the image.
- Debug captures named `*.failed.png` are unredacted. They stay in the script's temporary folder
  and must never be committed.
- If you can't run the capture, for example because the environment has no browser or can't
  download one, say so in your pull request. Name the shots that need regenerating, so that the
  README isn't left showing an outdated UI without anyone knowing.

[docs/screenshots.md](docs/screenshots.md) has the command, its options, the redaction rules, and
how to add a shot.

After changing project code or configuration, run `mise run check` before finishing and make sure the check passes. If the check fails, try `mise run fix` first, then fix any remaining issues manually.

During initial project setup, replace the remaining `[PLACEHOLDER]` release packaging task and documentation with the project's actual behavior.

This project is in early development and does not require backward compatibility yet. When a cleaner long-term design requires an incompatible change, make the change deliberately instead of preserving compatibility through extra complexity.

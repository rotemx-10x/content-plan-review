# Example plans

Every file here passes `scripts/plan_review.py validate`. Read the one closest to the job; copy
its shape, not its content.

| File | Shows |
|---|---|
| `deck-sections.plan.json` | One-subject slide deck. Sections as panels, a quiz with its answer slide linked both ways, a draft illustration, presenter notes and an English build note (`lang`) in `fields`, a flag, two questions. |
| `deck-sections.review-state.json` | A finished review of that deck: two Change marks, a Remove with a note, a hand edit, an added card to draft, a new order, one answer. Copy both files to a scratch folder and run `apply --dry-run` to see the summary. |
| `documents-tabs.plan.json` | A set of three documents as an outline. One tab per document, headings and exercises as cards (no numbers in titles), reference material as bullets rather than tables, plan-specific `chips`, an item in another language (`lang`), `addKind`, one question about a group. |
| `quiz-bank.plan.json` | A stand-alone question list. One tab per subject, quiz and poll `choices`, an Explanation field, `numbering: "continuous"` across tabs. |
| `session-plan.plan.json` | The plan of one meeting. Parts as panels; topics, exercises and a quiz as cards. |
| `deck-revision.plan.json` | A revision of published material. Closed sections for what does not change, a Current version field, `locks` on a card that must not be removed or moved. |

The examples use short ids such as `s01`. Real material uses the ids its build will use.

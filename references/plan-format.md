# Plan file format (`content-plan-review/1`)

The plan file is the single source of the review page. `scripts/plan_review.py build` injects it
into the page template, `apply` rewrites it from the owner's review, and `approve` writes the
approved plan as `PLAN.md`. `validate` is the authority on what is legal; this file explains what
each field is for. Working examples: `references/examples/`.

Contents: 1. Shape - 2. Top-level fields - 3. Groups - 4. Items - 5. Choices - 6. Figures -
7. Flags, links, locks - 8. Questions - 9. Text, direction and code - 10. Fields the script
writes - 11. What `validate` reports - 12. Limits

## 1. Shape

```
plan
  groups[]            a subject, a section, a document, a chapter
    groups[]          nested sections (at most 3 levels in total)
    items[]           a slide, a topic, a heading, a question: one card each
  questions[]         things the agent needs the owner to decide
```

Every group and every item becomes a card with Accept (the default), Change and Remove, text that
can be edited in place, and an instruction line.

## 2. Top-level fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Exactly `"content-plan-review/1"`. |
| `id` | yes | Stable slug for the plan, `[a-z0-9][a-z0-9-]{1,62}`. Never changes between rounds: the saved review and the browser copy are keyed on it. |
| `pageTitle` | yes | The page's name in the browser tab and the gallery: two to four words, at most 40 characters, in English. A name, not a summary. Keep it the same in every round. |
| `title` | yes | The headline, in the content language. |
| `planType` | yes | `deck`, `session-plan`, `documents`, `quiz` or `other`. Shown in the header as Slide deck, Session plan, Documents, Quiz or Plan. |
| `lang` | no | Language of the content, default `en`. Set it to the language the cards are written in: `he`, `ar`, `fa`, `ur` and `yi` lay the cards out from the right. See section 9. |
| `round` | yes | Integer from 1. `apply` raises it. Never edit it by hand. |
| `summary` | no | Two or three sentences: what will be generated, for whom, and how much it contains. |
| `facts` | no | `[{"label": "Audience", "value": "...", "lang": "he"}]`. Short label and value pairs under the summary; `lang` is optional (section 9). The page adds a live count of what is in the plan now. |
| `notesLang` | no | The language of all your notes to the owner, when every one of them is in that language: summary, facts, questions, flags and agent notes then read in its direction whatever letter they start with. Usually left out. See section 9. |
| `groups` | yes | The content tree. At least one group. |
| `questions` | no | See section 8. |
| `itemKinds` | no | `{"slide": {"label": "Slide", "plural": "slides", "chips": ["..."]}}`. Display name, plural for counts, and one-tap instructions per item kind. All three are optional. An undeclared kind is shown by its name with a capital letter. |
| `chips` | no | One-tap instructions for items of a kind that declares none. Without it the page offers: Make it shorter, Add an example, Split into two, Merge with the previous one, Turn into a quiz question. Set your own when those do not fit the content. |
| `groupChips` | no | Same, for group cards. Default: Make this section shorter, Split this section, Merge with the previous section, Move this section earlier. |
| `numbering` | no | Position numbers on item cards. `continuous` counts through the whole plan, across tabs too (the default without tabs). `per-tab` starts again in every top-level group (the default with tabs). `per-group` starts again in every group. `none` shows no numbers. Removed cards are skipped. |
| `secondLookDefault` | no | Initial state of "show me the reworked and new cards before building" on the finish sheet. Default `true`. Set `false` when the owner has said they do not want a second look. |

## 3. Groups

```json
{"id": "part-2", "display": "panel", "kind": "section", "title": "...", "summary": "...",
 "collapsed": false, "addKind": "slide", "items": [], "groups": []}
```

| Field | Meaning |
|---|---|
| `id` | Unique across every group, item and question. `[A-Za-z0-9][A-Za-z0-9_-]{0,47}`. Never start an id you write with `new-`: the page uses that prefix for cards the owner adds, and those cards keep it in later rounds. Keep ids stable between rounds. |
| `display` | `tab` or `panel` (default `panel`). `tab` is legal only at the top level, and then every top-level group must be a tab. |
| `kind` | A word for what the group is: `subject`, `section`, `document`, `chapter`. Shown as a small tag, and used on the buttons ("Add section"). |
| `title` | Required. Editable on the page. |
| `summary` | One or two lines on what the group covers. Editable on the page. |
| `collapsed` | Start a panel closed. Default open. A tab ignores it. |
| `addKind` | Item kind used by "Add ..." in this group. Default: the kind of its last item. |
| `items`, `groups` | Children. A group may hold both; items are shown first. |

A group has no `label`. A document type or a similar tag goes in `kind` or in the summary.

**Tabs or panels.** Use tabs for different subjects that are reviewed one at a time: the subjects of
a deck that covers several, the documents of a document set, the parts of a question bank. Use
panels for the consecutive sections of one deck, one session or one document. A plan with one
subject has no tabs, only panels. Inside a tab, sections are panels again. Keep tabs to about
eight; beyond that, group them.

## 4. Items

```json
{"id": "s07", "kind": "slide", "label": "split - figure", "title": "...", "body": "...",
 "fields": [{"key": "notes", "label": "Presenter notes", "value": "...", "open": false}],
 "choices": {}, "figure": {}, "flags": [], "links": [], "locks": {}, "lang": "en"}
```

| Field | Meaning |
|---|---|
| `id` | Same rules as a group id. |
| `kind` | Required, `[a-z][a-z0-9-]*`. What the card is: `slide`, `quiz`, `poll`, `topic`, `heading`, `document`, `exercise`, or any other word. Declare it in `itemKinds` to control its display name. |
| `label` | Short technical tag shown beside the kind: a slide template, a document type. Not editable. |
| `title` | Required. Editable. |
| `body` | What the item will contain, in plain text. Line breaks are kept. Editable. |
| `fields` | Extra named text blocks under a disclosure (presenter notes, build notes, sources). Each needs `key` (`[A-Za-z][A-Za-z0-9_-]*`, unique in the item), `label` and `value`. Each value is editable. `open: true` shows it expanded. `lang` forces the field's direction, for example `"en"` on English build notes in a Hebrew plan. |
| `lang` | Forces the direction of this item's texts when it differs from the plan's. |

Write the card so it can be judged without opening anything else: the title as it will appear, and
a body that says what the audience will see or learn, not how it will be built. Build detail goes
into `fields`.

## 5. Choices (quiz and poll items)

```json
"choices": {"kind": "quiz", "prompt": "...", "options": [{"id": "a", "text": "..."},
            {"id": "b", "text": "..."}], "correct": "a"}
```

`kind` is `quiz` (needs `correct`, one of the option ids) or `poll` (must not carry `correct`). Two
to twenty options with unique ids. The owner can edit the prompt and every option, add or delete
options and pick the correct answer; the page keeps the count between two and twenty and does not
let the correct option be deleted.

`choices` is optional on every item, whatever its kind. A question with no options, such as a
numeric estimate or a word cloud, is an item with its prompt in the body and no `choices`.

## 6. Figures (draft illustrations)

```json
"figure": {"svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 640 360\">...</svg>",
           "caption": "...", "alt": "..."}
```

Give exactly one of `svg` (a complete, self-contained SVG that starts with `<svg`: no scripts, no
external files, no classes that rely on page CSS) or `dataUri` (a `data:image/...` URI). The page
shows it as an image, so page fonts do not apply inside it and nothing in it can run. Keep one
figure under about 150 KB. The caption is not editable; the owner changes a figure through the
card's instruction line.

## 7. Flags, links, locks

- `flags`: `[{"tone": "info", "text": "...", "lang": "en"}]`, tone `info` or `warn`, `lang`
  optional (section 9). Things the owner should see before deciding: "depends on the demo
  working", "new compared with last year".
- `links`: `[{"to": "s21", "label": "Answer slide", "needs": true}]`. A chip that jumps to another
  card. `needs: true` says this item breaks without the target; the page warns when the target is
  removed, and `apply` repeats the warning. `to` must be an existing id. Two cards may need each
  other, such as a quiz and its answer slide.
- `locks`: `{"remove": "reason", "move": "reason", "edit": "reason"}`. Disables that action and
  shows the reason. Use them when revising published material where an action would do damage.

Groups accept `flags` and `locks` too, but not `links`.

## 8. Questions

```json
{"id": "q1", "question": "...", "detail": "...", "about": "s12",
 "options": [{"id": "a", "label": "...", "detail": "..."}, {"id": "b", "label": "..."}],
 "recommended": "a", "lang": "en"}
```

One card per open decision, with the recommended answer pre-selected and a free note line. At
least two options; `recommended` is one of the option ids. `about` (optional) links the question
to any card or group by id. `lang` (optional) sets the direction of the question and its
options (section 9). Without tabs the questions open the page as the first panel; with
tabs they get a Questions tab of their own and a notice that points at it.

Ask only what changes the plan. A question the owner leaves alone is answered with `recommended`,
so make sure the recommended answer is the one the plan is already written for.

## 9. Text, direction and code

- Every text is plain text. No HTML and no Markdown: tags would be shown as typed.
- Text between a pair of backticks is shown as left-to-right code, in every text on the card:
  titles, bodies, summaries, fields, options, flags. A pair may span lines, and spaces and line
  breaks are kept, so a short snippet can be one pair. When a text holds an odd number of
  backticks, none of them counts and the whole text is shown as typed, pairs included;
  `validate` warns about it.
- The page's own labels are always English, left to right.
- The plan's `lang` decides which side the cards align to. Card text (titles, bodies, summaries,
  fields, options) reads right to left when it holds any Hebrew or Arabic letter outside code,
  and left to right otherwise. A Hebrew sentence that opens with an English term therefore still
  reads correctly, and an English build note inside a Hebrew plan reads left to right while
  staying on the plan's side.
- Your notes to the owner (`summary`, `facts`, `questions`, `flags`, `agentNote`) read in the
  direction of their first letter outside code, so an English note that quotes a Hebrew title
  still reads left to right. Start a note with a word in its own language.
- Overrides: an item's `lang` forces the direction of that item's texts; a field's `lang` forces
  that field; `lang` on a fact, flag or question forces that one; `notesLang` forces every note
  that has no `lang` of its own.

## 10. Fields the script writes (do not author these in round 1)

| Field | On | Written by | Meaning |
|---|---|---|---|
| `pending` | item, group, plan | `apply` | `{"instruction": "...", "draft": true}`. Work the agent owes: rework as instructed, or draft a new card. On the plan it may also hold `followUps`, a list of lines: answers the owner gave (a choice that differs from the recommended one, or a note), and notes left on removed cards. `validate` and `build` refuse a plan that still has any `pending`. Delete each one when its work is done. |
| `state` | item, group | `apply`, agent | `new` (added in the last round), `reworked` (the agent changed it), `proofread` (the owner's own text with typos fixed). Shown as a tag in the next round. `apply` clears the old values. |
| `agentNote` | item, group | agent | One line on what was changed and why. Shown on the card in the next round in a box labelled Claude. |
| `ownerEdited` | item, group | `apply` | `["title", "body", "field:notes", "choices"]`: text the owner wrote by hand. Keep the wording; fix only typos and style rule violations. |
| `copyOf` | item | `apply` | The id of the card this one was duplicated from. |
| `decisions` | plan | `apply` | Answered questions: `{"id", "question", "choice", "label", "note", "by": "owner" or "default", "round"}`. The page does not show them; `PLAN.md` does. |
| `approved` | plan | `approve` | `{"at": "...", "afterRound": N}`. |

## 11. What `validate` reports

Errors stop `build`, `published` and `apply`: a missing or malformed required field, a duplicate
id, a link or `about` that points nowhere, tabs mixed with panels at the top level, a tab below the
top level, more than three levels of groups, a quiz without a valid `correct`, a figure with both
or neither of `svg` and `dataUri`, an em dash or en dash in `pageTitle`, and any `pending` (unless
`--allow-pending`).

Warnings do not stop anything, but read them: an unknown field (ignored, with a "did you mean"
hint for a likely typo), an empty group, an em dash or en dash in any text, an odd number of
backticks in a text, a placeholder (`TODO`, `TBD`, `FIXME`, `XXX`, להשלמה) in a card's text, more
than 250 items, more than 8 tabs, a figure over 150 KB, a plan file over 2 MB. `approve` repeats
the placeholder warning.

## 12. Limits

- The page is comfortable up to about 250 items. Split a larger plan into two reviews.
- The plan file should stay under 2 MB; figures are the only thing that can make it large. The
  built page must stay under 15 MB.
- One review folder holds one plan. A second plan gets its own folder.

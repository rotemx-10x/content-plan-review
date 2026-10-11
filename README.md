# content-plan-review

A [Claude Code](https://code.claude.com) skill: before Claude builds a slide deck, a lesson
plan, a set of documents or a quiz, it shows you the plan as a review page. Every slide,
section, document or question is a card with Accept (the default), Change and Remove. You can
edit text in place, add and reorder cards, and leave an instruction on any card. When you press
Finish review, Claude reads your marks back, applies them, reworks what you marked, and only
then builds.

## Requirements

- Claude Code
- git
- Python 3.9 or later (standard library only, nothing to install)
- For the review page: a Claude Code session that can publish pages to claude.ai (the Artifact
  tool). Without it the skill builds the same page as a local HTML file: open it in a browser,
  review, press Download, and tell Claude where the file went.

## Install

The skill is this whole repository, cloned into Claude Code's skills folder under the name
`content-plan-review`.

macOS and Linux:

```bash
git clone https://github.com/rotemx-10x/content-plan-review.git ~/.claude/skills/content-plan-review
```

Windows (PowerShell):

```powershell
git clone https://github.com/rotemx-10x/content-plan-review.git "$env:USERPROFILE\.claude\skills\content-plan-review"
```

To install it for one project only, clone it into that project's `.claude/skills/` folder
instead.

## Check that it loaded

Open a new Claude Code session and type `/content-plan-review`. The skill shows up in the menu.
Claude also uses it on its own when you ask it to plan a deck, a lesson, documents or a quiz.

## Update

```bash
git -C ~/.claude/skills/content-plan-review pull
```

On Windows: `git -C "$env:USERPROFILE\.claude\skills\content-plan-review" pull`

## Remove

Delete the folder `~/.claude/skills/content-plan-review`
(`%USERPROFILE%\.claude\skills\content-plan-review` on Windows).

## What is in the repository

| Path | What it is |
|---|---|
| `SKILL.md` | The skill itself: when to run it and the review loop, step by step. |
| `scripts/plan_review.py` | Validates a plan, builds the review page, reads your marks back and applies them. |
| `assets/review-page.template.html` | The review page. |
| `references/plan-format.md` | Every field of a plan file. |
| `references/review-state.md` | What your marks look like when Claude reads them back. |
| `references/examples/` | Example plans: a deck, a document set, a quiz bank, a lesson plan, a revision, and one finished review. |

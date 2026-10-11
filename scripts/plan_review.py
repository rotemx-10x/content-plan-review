#!/usr/bin/env python3
"""Helper for the content-plan-review skill: publish a content plan as a review page, read the
owner's review back and apply it to the plan.

Subcommands:
  validate  <plan.json> [--allow-pending] [--json]
            Check the plan file against the format. Errors exit 1.
  build     <plan.json> --out <page.html> [--template <path>] [--standalone]
            Inject the plan into the page template and write the page.
  published <plan.json> --url <artifact url> [--description <sentence>]
            Record that the current round was published, and keep a copy of it.
  apply     <plan.json> --state <file-or-dir> [--allow-unfinished] [--reapply] [--dry-run]
            Apply the owner's review (content-plan-review-state/1) to the plan.
  approve   <plan.json> [--out <PLAN.md>]
            Mark the plan approved and write PLAN.md.
  render    <plan.json> [--out <PLAN.md>]
            Write the plan as readable Markdown.
  status    <plan.json>
            Where the review stands.

The review folder is the directory that holds plan.json; review.json and rounds/ live beside it.
JSON results go to stdout, human-readable errors and notes to stderr.
Exit codes: 0 ok, 1 invalid input or usage, 2 this round was already applied and the review
changed since, 3 review not finished.

Standard library only; runs on Python 3.9 and later.
"""

import argparse
import copy
import datetime
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import sys

# What the published page declares, and its tab icon. Adjusted by the skill owner.
CAPABILITIES = {"db": {}, "user": {}, "downloads": True, "comments": {}, "room": {}}
ICON = "checklist"

PLAN_SCHEMA = "content-plan-review/1"
STATE_SCHEMA = "content-plan-review-state/1"
PLAN_TOKEN = "__CPR_PLAN_JSON__"
TITLE_TOKEN = "__CPR_PAGE_TITLE__"
DEFAULT_TEMPLATE = os.path.join("..", "assets", "review-page.template.html")

STANDALONE_PREFIX = (
    '<!doctype html><html><head><meta charset=utf8>'
    '<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
    '<style>:root{color-scheme:light;box-sizing:border-box;'
    'padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
    'html{scroll-padding-top:env(safe-area-inset-top,0px)}'
    'body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;'
    'background:#faf9f5;color:#141413}'
    'img{max-width:100%}'
    '[hidden]:not([hidden=until-found i]){display:none!important}'
    '</style></head><body>'
)
STANDALONE_SUFFIX = "</body></html>"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_CHANGED = 2
EXIT_UNFINISHED = 3

MAX_PAGE_BYTES = 15 * 1024 * 1024
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_FIGURE_BYTES = 150 * 1024
MAX_ITEMS = 250
MAX_TABS = 8
MAX_DEPTH = 3
MAX_SCAN_FILES = 2000

DASHES = ("\u2014", "\u2013")  # em dash, en dash

PLAN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
CARD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
KIND_RE = re.compile(r"[a-z][a-z0-9-]*")
FIELD_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
ARTIFACT_URL_RE = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+/?(?:[?#].*)?$")

PLAN_TYPES = ("deck", "session-plan", "documents", "quiz", "other")
NUMBERINGS = ("continuous", "per-tab", "per-group", "none")
DISPLAYS = ("tab", "panel")
CARD_STATES = ("new", "reworked", "proofread")
TONES = ("info", "warn")
LOCK_KEYS = ("remove", "move", "edit")

PLAN_FIELDS = ("schema", "id", "pageTitle", "title", "planType", "lang", "round", "summary",
               "facts", "groups", "questions", "itemKinds", "chips", "groupChips", "numbering",
               "secondLookDefault", "notesLang", "pending", "decisions", "approved")
GROUP_FIELDS = ("id", "display", "kind", "title", "summary", "collapsed", "addKind", "items",
                "groups", "flags", "locks", "pending", "state", "agentNote", "ownerEdited")
ITEM_FIELDS = ("id", "kind", "label", "title", "body", "fields", "choices", "figure", "flags",
               "links", "locks", "lang", "pending", "state", "agentNote", "ownerEdited", "copyOf")
QUESTION_FIELDS = ("id", "question", "detail", "about", "options", "recommended", "lang")

# Keys a tool may wrap the saved review document in; tried first when unwrapping.
WRAP_KEYS = ("data", "doc", "document", "fields", "value")


class CprError(Exception):
    """Ends the command: the message goes to stderr and the process exits with `code`."""

    def __init__(self, message, code=EXIT_INVALID):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- input and output

def _write_stream(stream, text):
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        return
    stream.flush()
    buffer.write(text.encode("utf-8", "replace"))
    buffer.flush()


def note(message):
    """One human-readable line on stderr."""
    _write_stream(sys.stderr, message.rstrip("\n") + "\n")


def dump_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def emit(obj):
    """The machine-readable result on stdout."""
    _write_stream(sys.stdout, dump_json(obj))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_text(path, what, newline=None):
    try:
        with open(path, "r", encoding="utf-8", newline=newline) as handle:
            return handle.read()
    except FileNotFoundError:
        raise CprError(f"{what} not found: {path}")
    except IsADirectoryError:
        raise CprError(f"{what} is a directory, not a file: {path}")
    except UnicodeDecodeError as exc:
        raise CprError(f"{what} is not UTF-8 text: {path} ({exc})")
    except OSError as exc:
        raise CprError(f"cannot read {what} {path}: {exc}")


def read_json_file(path, what):
    text = read_text(path, what)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CprError(f"{what} is not valid JSON: {path} ({exc})")


def write_text(path, text):
    """Write UTF-8 text through a temporary file, so a failed write leaves the old file whole."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def write_json(path, obj):
    write_text(path, dump_json(obj))


# ---------------------------------------------------------------- small helpers

def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_text(value):
    """A string with something other than whitespace in it."""
    return isinstance(value, str) and value.strip() != ""


def is_str_list(value):
    return isinstance(value, list) and all(isinstance(entry, str) for entry in value)


def has_dash(text):
    return any(dash in text for dash in DASHES)


def text_of(value):
    return value if isinstance(value, str) else ""


def one_line(value):
    """Text on one line, for list entries in generated Markdown."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def plural(count, word, words=None):
    return f"{count} {word if count == 1 else (words or word + 's')}"


def ref(card_id, title):
    """A card named in generated Markdown: its id, then its title."""
    text = one_line(title)
    return f"`{card_id}` {text}" if text else f"`{card_id}` (untitled)"


class Paths(object):
    """The review folder: plan.json, review.json and rounds/ side by side."""

    def __init__(self, plan_path):
        self.plan = os.path.abspath(plan_path)
        self.folder = os.path.dirname(self.plan)
        self.review = os.path.join(self.folder, "review.json")
        self.rounds = os.path.join(self.folder, "rounds")

    def round_file(self, number, suffix):
        return os.path.join(self.rounds, f"round-{number}.{suffix}")


def walk_cards(groups, depth=1, parent=None, path="$.groups"):
    """Yield (type, card, depth, parent group, json path) in page order: each group, then its
    items, then its sub-groups. Entries that are not objects are skipped."""
    if not isinstance(groups, list):
        return
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        group_path = f"{path}[{index}]"
        yield "group", group, depth, parent, group_path
        items = group.get("items")
        if isinstance(items, list):
            for position, item in enumerate(items):
                if isinstance(item, dict):
                    yield "item", item, depth, group, f"{group_path}.items[{position}]"
        for card in walk_cards(group.get("groups"), depth + 1, group, group_path + ".groups"):
            yield card


def has_pending(card):
    return isinstance(card, dict) and card.get("pending") is not None


def is_draft(card):
    pending = card.get("pending") if isinstance(card, dict) else None
    return isinstance(pending, dict) and pending.get("draft") is True


def collect_pending(plan):
    """[(type, id, title, pending)] for the plan and every card that still owes work."""
    found = []
    if has_pending(plan):
        found.append(("plan", plan.get("id"), plan.get("title"), plan.get("pending")))
    for kind, card, _depth, _parent, _path in walk_cards(plan.get("groups")):
        if has_pending(card):
            found.append((kind, card.get("id"), card.get("title"), card.get("pending")))
    return found


def pending_label(found):
    ids = ["(plan)" if kind == "plan" else str(card_id) for kind, card_id, _t, _p in found]
    verb = "card still waits" if len(found) == 1 else "cards still wait"
    return f"{len(found)} {verb} for rework: {', '.join(ids)}"


# ---------------------------------------------------------------- validate

class Report(object):
    """Errors fail validation; warnings do not. Both are (json path, message)."""

    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, path, message):
        self.errors.append((path, message))

    def warn(self, path, message):
        self.warnings.append((path, message))


class _Context(object):
    def __init__(self):
        self.ids = {}
        self.links = []
        self.abouts = []
        self.counts = {"items": 0, "groups": 0, "questions": 0, "tabs": 0, "pending": 0}


def validate_plan(plan, allow_pending=False, file_bytes=None):
    """Check a plan against plan-format.md. Returns (Report, counts)."""
    rep = Report()
    ctx = _Context()
    if not isinstance(plan, dict):
        rep.error("$", "the plan must be a JSON object")
        return rep, ctx.counts
    _check_top(plan, rep)
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        rep.error("$.groups", "is required: a list with at least one group")
    else:
        _check_top_displays(groups, rep, ctx)
        _check_groups(groups, 1, "$.groups", rep, ctx)
    if "questions" in plan:
        _check_questions(plan["questions"], rep, ctx)
    for path, target in ctx.links:
        if target not in ctx.ids:
            rep.error(path, f'links to "{target}", which is not an id in this plan')
    for path, target in ctx.abouts:
        if target not in ctx.ids:
            rep.error(path, f'points at "{target}", which is not an id in this plan')
    found = collect_pending(plan)  # the plan itself counts: a planInstruction is work too
    ctx.counts["pending"] = len(found)
    if found and not allow_pending:
        rep.error("$", pending_label(found)
                  + " (rework them and delete each pending; build refuses a plan that has any)")
    if ctx.counts["items"] > MAX_ITEMS:
        rep.warn("$", f"{ctx.counts['items']} items: the page is comfortable up to about "
                      f"{MAX_ITEMS}; split a larger plan into two reviews")
    if ctx.counts["tabs"] > MAX_TABS:
        rep.warn("$.groups", f"{ctx.counts['tabs']} tabs: keep tabs to about {MAX_TABS}; "
                             "beyond that, group them")
    if file_bytes is not None and file_bytes > MAX_PLAN_BYTES:
        rep.warn("$", f"the plan file is {file_bytes} bytes, over {MAX_PLAN_BYTES} (2 MB); "
                      "figures are usually the cause")
    owners = _dash_owners(plan)
    if owners:
        first = []
        for owner in owners:
            if owner not in first:
                first.append(owner)
        rep.warn("$", f"em dash or en dash in {plural(len(owners), 'text')}, use a plain hyphen; "
                      f"first in: {', '.join(first[:5])}")
    odd = [where for where, text in shown_texts(plan) if text.count("`") % 2]
    if odd:
        rep.warn("$", f"an odd number of backticks in {plural(len(odd), 'text')}: the page then "
                      f"shows that whole text as typed, code pairs included; first in: "
                      f"{', '.join(odd[:5])}")
    held = placeholder_owners(plan)
    if held:
        rep.warn("$", f"a placeholder (TODO, TBD, להשלמה) in {plural(len(held), 'text')}: a build "
                      f"usually rejects it, so plan around the missing value or ask for it; first "
                      f"in: {', '.join(held[:5])}")
    return rep, ctx.counts


PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|FIXME|XXX)\b|להשלמה")


def card_texts(plan):
    """(where, text) for the text of every card: the part of the plan a build uses."""
    out = []
    for kind, card, _depth, _parent, _path in walk_cards(plan.get("groups")):
        owner = str(card.get("id"))
        texts = [(key, card.get(key)) for key in ("title", "summary", "label", "body")]
        if kind == "item":
            texts += [(f"field {field.get('key')}", field.get("value"))
                      for field in card.get("fields") or [] if isinstance(field, dict)]
            choices = card.get("choices")
            if isinstance(choices, dict):
                texts.append(("prompt", choices.get("prompt")))
                texts += [(f"option {option.get('id')}", option.get("text"))
                          for option in choices.get("options") or [] if isinstance(option, dict)]
        out.extend((f"{owner} {key}", value) for key, value in texts if isinstance(value, str) and value)
    return out


def placeholder_owners(plan):
    """Where a card still holds a placeholder such as TODO, TBD or להשלמה."""
    return [where for where, text in card_texts(plan) if PLACEHOLDER_RE.search(text)]


def shown_texts(plan):
    """(where, text) for every text the page shows from the plan, in page order."""
    out = []

    def add(where, value):
        if isinstance(value, str) and value:
            out.append((where, value))

    def flags_of(card, owner):
        for flag in card.get("flags") or []:
            if isinstance(flag, dict):
                add(f"{owner} flag", flag.get("text"))

    add("plan title", plan.get("title"))
    add("plan summary", plan.get("summary"))
    for fact in plan.get("facts") or []:
        if isinstance(fact, dict):
            add("plan fact", fact.get("label"))
            add("plan fact", fact.get("value"))
    for kind, card, _depth, _parent, _path in walk_cards(plan.get("groups")):
        owner = str(card.get("id"))
        for key in ("title", "summary", "label", "body", "agentNote"):
            add(f"{owner} {key}", card.get(key))
        flags_of(card, owner)
        if kind != "item":
            continue
        for field in card.get("fields") or []:
            if isinstance(field, dict):
                add(f"{owner} field {field.get('key')}", field.get("value"))
        choices = card.get("choices")
        if isinstance(choices, dict):
            add(f"{owner} prompt", choices.get("prompt"))
            for option in choices.get("options") or []:
                if isinstance(option, dict):
                    add(f"{owner} option {option.get('id')}", option.get("text"))
        figure = card.get("figure")
        if isinstance(figure, dict):
            add(f"{owner} caption", figure.get("caption"))
    for question in plan.get("questions") or []:
        if not isinstance(question, dict):
            continue
        owner = str(question.get("id"))
        add(f"{owner} question", question.get("question"))
        add(f"{owner} detail", question.get("detail"))
        for option in question.get("options") or []:
            if isinstance(option, dict):
                add(f"{owner} option {option.get('id')}", option.get("label"))
                add(f"{owner} option {option.get('id')}", option.get("detail"))
    return out


def _check_top(plan, rep):
    if plan.get("schema") != PLAN_SCHEMA:
        rep.error("$.schema", f'must be exactly "{PLAN_SCHEMA}"')
    plan_id = plan.get("id")
    if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
        rep.error("$.id", "must be a slug matching [a-z0-9][a-z0-9-]{1,62}")
    _check_page_title(plan, rep)
    if not is_text(plan.get("title")):
        rep.error("$.title", "is required and must not be empty")
    if plan.get("planType") not in PLAN_TYPES:
        rep.error("$.planType", "must be one of: " + ", ".join(PLAN_TYPES))
    number = plan.get("round")
    if not is_int(number) or number < 1:
        rep.error("$.round", "must be an integer of 1 or more")
    for key in ("lang", "notesLang"):
        if key in plan and not isinstance(plan[key], str):
            rep.error(f"$.{key}", "must be a string such as he or en")
    if "numbering" in plan and plan["numbering"] not in NUMBERINGS:
        rep.error("$.numbering", "must be one of: " + ", ".join(NUMBERINGS))
    if "secondLookDefault" in plan and not isinstance(plan["secondLookDefault"], bool):
        rep.error("$.secondLookDefault", "must be true or false")
    if "summary" in plan and not isinstance(plan["summary"], str):
        rep.error("$.summary", "must be a string")
    for key in ("chips", "groupChips"):
        if key in plan and not is_str_list(plan[key]):
            rep.error(f"$.{key}", "must be a list of strings")
    if "facts" in plan:
        _check_facts(plan["facts"], rep)
    if "itemKinds" in plan:
        _check_item_kinds(plan["itemKinds"], rep)
    decisions = plan.get("decisions")
    if "decisions" in plan and not (isinstance(decisions, list)
                                    and all(isinstance(entry, dict) for entry in decisions)):
        rep.error("$.decisions", "must be a list of objects")
    if "approved" in plan and not isinstance(plan["approved"], dict):
        rep.error("$.approved", "must be an object")
    if has_pending(plan):
        _check_pending_shape(plan["pending"], "$.pending", rep)
    _unknown_fields(plan, PLAN_FIELDS, "$", rep)


def _check_page_title(plan, rep):
    if "pageTitle" not in plan:
        rep.error("$.pageTitle", "is required: a name of two to four words")
        return
    title = plan["pageTitle"]
    if not isinstance(title, str):
        rep.error("$.pageTitle", "must be a string")
        return
    words = len(title.split())
    if not 2 <= words <= 4:
        rep.error("$.pageTitle", f"must be two to four words (found {words})")
    if len(title) > 40:
        rep.error("$.pageTitle", f"must be at most 40 characters (found {len(title)})")
    if has_dash(title):
        rep.error("$.pageTitle", "must not contain an em dash or en dash; use a plain hyphen")


def _check_facts(facts, rep):
    if not isinstance(facts, list):
        rep.error("$.facts", "must be a list of {label, value}")
        return
    for index, fact in enumerate(facts):
        if not (isinstance(fact, dict) and isinstance(fact.get("label"), str)
                and isinstance(fact.get("value"), str)):
            rep.error(f"$.facts[{index}]", "must be an object with a string label and value")
        elif "lang" in fact and not isinstance(fact["lang"], str):
            rep.error(f"$.facts[{index}].lang", "must be a string such as he or en")


def _check_item_kinds(kinds, rep):
    if not isinstance(kinds, dict):
        rep.error("$.itemKinds", "must be an object mapping a kind to {label, plural, chips}")
        return
    for kind, spec in kinds.items():
        path = f"$.itemKinds.{kind}"
        if not isinstance(spec, dict):
            rep.error(path, "must be an object {label, plural, chips}")
            continue
        for key in ("label", "plural"):
            if key in spec and not isinstance(spec[key], str):
                rep.error(f"{path}.{key}", "must be a string")
        if "chips" in spec and not is_str_list(spec["chips"]):
            rep.error(f"{path}.chips", "must be a list of strings")


def _unknown_fields(obj, known, path, rep):
    for key in obj:
        if key in known:
            continue
        guess = difflib.get_close_matches(key, known, n=1, cutoff=0.75)
        hint = f' (did you mean "{guess[0]}"?)' if guess else ""
        rep.warn(f"{path}.{key}", f"unknown field, ignored{hint}")


def _check_top_displays(groups, rep, ctx):
    top = [group for group in groups if isinstance(group, dict)]
    tabs = [group for group in top if group.get("display") == "tab"]
    ctx.counts["tabs"] = len(tabs)
    if tabs and len(tabs) != len(top):
        others = [str(group.get("id")) for group in top if group.get("display") != "tab"]
        rep.error("$.groups", "mixes tabs and panels: when one top-level group is a tab, every "
                              "top-level group must be (not tabs: " + ", ".join(others) + ")")


def _check_card_id(card, path, rep, ctx):
    card_id = card.get("id")
    if not isinstance(card_id, str):
        rep.error(path + ".id", "is required: a string matching [A-Za-z0-9][A-Za-z0-9_-]{0,47}")
        return
    if not CARD_ID_RE.fullmatch(card_id):
        rep.error(path + ".id", f'"{card_id}" does not match [A-Za-z0-9][A-Za-z0-9_-]{{0,47}}')
    first = ctx.ids.get(card_id)
    if first is not None:
        rep.error(path + ".id", f'duplicate id "{card_id}" (first used at {first}); ids are '
                                "unique across all groups, items and questions")
    else:
        ctx.ids[card_id] = path


def _check_title(card, path, rep):
    title = card.get("title")
    if is_draft(card):
        if title is not None and not isinstance(title, str):
            rep.error(path + ".title", "must be a string")
    elif not is_text(title):
        rep.error(path + ".title", "is required and must not be empty")


def _check_pending_shape(pending, path, rep):
    if not isinstance(pending, dict):
        rep.error(path, 'must be an object {"instruction": "...", "draft": true}')
        return
    if "instruction" in pending and not isinstance(pending["instruction"], str):
        rep.error(path + ".instruction", "must be a string")
    if "draft" in pending and not isinstance(pending["draft"], bool):
        rep.error(path + ".draft", "must be true or false")
    if "followUps" in pending and not is_str_list(pending["followUps"]):
        rep.error(path + ".followUps", "must be a list of strings")


def _check_common(card, path, rep, ctx):
    """Fields that groups and items share."""
    if "state" in card and card["state"] not in CARD_STATES:
        rep.error(path + ".state", "must be one of: " + ", ".join(CARD_STATES))
    if "agentNote" in card and not isinstance(card["agentNote"], str):
        rep.error(path + ".agentNote", "must be a string")
    if "ownerEdited" in card and not is_str_list(card["ownerEdited"]):
        rep.error(path + ".ownerEdited", "must be a list of strings")
    if "flags" in card:
        _check_flags(card["flags"], path + ".flags", rep)
    if "locks" in card:
        _check_locks(card["locks"], path + ".locks", rep)
    if has_pending(card):
        _check_pending_shape(card["pending"], path + ".pending", rep)


def _check_flags(flags, path, rep):
    if not isinstance(flags, list):
        rep.error(path, "must be a list of {tone, text}")
        return
    for index, flag in enumerate(flags):
        flag_path = f"{path}[{index}]"
        if not isinstance(flag, dict):
            rep.error(flag_path, "must be an object {tone, text}")
            continue
        if flag.get("tone") not in TONES:
            rep.error(flag_path + ".tone", "must be info or warn")
        if not is_text(flag.get("text")):
            rep.error(flag_path + ".text", "must be a non-empty string")
        if "lang" in flag and not isinstance(flag["lang"], str):
            rep.error(flag_path + ".lang", "must be a string such as he or en")


def _check_locks(locks, path, rep):
    if not isinstance(locks, dict):
        rep.error(path, "must be an object such as {\"remove\": \"reason\"}")
        return
    for key, value in locks.items():
        if key not in LOCK_KEYS:
            rep.error(f"{path}.{key}", "is not a lock: use remove, move or edit")
        elif not isinstance(value, str):
            rep.error(f"{path}.{key}", "must be a string: the reason shown to the owner")


def _check_groups(groups, depth, path, rep, ctx):
    for index, group in enumerate(groups):
        group_path = f"{path}[{index}]"
        if not isinstance(group, dict):
            rep.error(group_path, "a group must be an object")
            continue
        ctx.counts["groups"] += 1
        if depth > MAX_DEPTH:
            rep.error(group_path, f"groups nest at most {MAX_DEPTH} levels deep")
        _check_card_id(group, group_path, rep, ctx)
        _check_title(group, group_path, rep)
        display = group.get("display")
        if "display" in group and display not in DISPLAYS:
            rep.error(group_path + ".display", "must be tab or panel")
        elif display == "tab" and depth > 1:
            rep.error(group_path + ".display", '"tab" is legal only at the top level; use "panel"')
        for key in ("kind", "summary"):
            if key in group and not isinstance(group[key], str):
                rep.error(f"{group_path}.{key}", "must be a string")
        add_kind = group.get("addKind")
        if "addKind" in group and not (isinstance(add_kind, str) and KIND_RE.fullmatch(add_kind)):
            rep.error(group_path + ".addKind", "must be an item kind matching [a-z][a-z0-9-]*")
        if "collapsed" in group and not isinstance(group["collapsed"], bool):
            rep.error(group_path + ".collapsed", "must be true or false")
        _check_common(group, group_path, rep, ctx)
        items = group.get("items")
        subgroups = group.get("groups")
        if "items" in group and not isinstance(items, list):
            rep.error(group_path + ".items", "must be a list")
        elif isinstance(items, list):
            _check_items(items, group_path + ".items", rep, ctx)
        if "groups" in group and not isinstance(subgroups, list):
            rep.error(group_path + ".groups", "must be a list")
        elif isinstance(subgroups, list):
            _check_groups(subgroups, depth + 1, group_path + ".groups", rep, ctx)
        if not items and not subgroups:
            rep.warn(group_path, f"group {group.get('id')} is empty")
        _unknown_fields(group, GROUP_FIELDS, group_path, rep)


def _check_items(items, path, rep, ctx):
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            rep.error(item_path, "an item must be an object")
            continue
        ctx.counts["items"] += 1
        _check_card_id(item, item_path, rep, ctx)
        kind = item.get("kind")
        if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
            rep.error(item_path + ".kind", "is required and must match [a-z][a-z0-9-]*")
        _check_title(item, item_path, rep)
        for key in ("body", "label", "lang", "copyOf"):
            if key in item and not isinstance(item[key], str):
                rep.error(f"{item_path}.{key}", "must be a string")
        if "fields" in item:
            for sub, message in fields_problems(item["fields"]):
                rep.error(item_path + ".fields" + sub, message)
        if "choices" in item:
            for sub, message in choices_problems(item["choices"]):
                rep.error(item_path + ".choices" + sub, message)
        if "figure" in item:
            _check_figure(item["figure"], item_path + ".figure", rep)
        if "links" in item:
            _check_links(item["links"], item_path + ".links", rep, ctx)
        _check_common(item, item_path, rep, ctx)
        _unknown_fields(item, ITEM_FIELDS, item_path, rep)


def fields_problems(fields):
    """Problems with an item's fields as [(relative json path, message)]; empty when fine."""
    if not isinstance(fields, list):
        return [("", "must be a list of {key, label, value}")]
    problems = []
    seen = set()
    for index, field in enumerate(fields):
        path = f"[{index}]"
        if not isinstance(field, dict):
            problems.append((path, "must be an object {key, label, value}"))
            continue
        key = field.get("key")
        if not isinstance(key, str) or not FIELD_KEY_RE.fullmatch(key):
            problems.append((path + ".key", "must match [A-Za-z][A-Za-z0-9_-]*"))
        elif key in seen:
            problems.append((path + ".key", f'duplicate key "{key}" in this item'))
        else:
            seen.add(key)
        if not isinstance(field.get("label"), str):
            problems.append((path + ".label", "is required and must be a string"))
        if not isinstance(field.get("value"), str):
            problems.append((path + ".value", "is required and must be a string"))
        if "open" in field and not isinstance(field["open"], bool):
            problems.append((path + ".open", "must be true or false"))
        if "lang" in field and not isinstance(field["lang"], str):
            problems.append((path + ".lang", "must be a string such as he or en"))
    return problems


def choices_problems(choices):
    """Problems with a choices object as [(relative json path, message)]; empty when fine."""
    if not isinstance(choices, dict):
        return [("", "must be an object {kind, prompt, options, correct}")]
    problems = []
    kind = choices.get("kind")
    if kind not in ("quiz", "poll"):
        problems.append((".kind", "must be quiz or poll"))
    if "prompt" in choices and not isinstance(choices["prompt"], str):
        problems.append((".prompt", "must be a string"))
    options = choices.get("options")
    ids = []
    if not isinstance(options, list):
        problems.append((".options", "must be a list of 2 to 20 options {id, text}"))
    else:
        if not 2 <= len(options) <= 20:
            problems.append((".options", f"must hold 2 to 20 options (found {len(options)})"))
        for index, option in enumerate(options):
            path = f".options[{index}]"
            if not isinstance(option, dict):
                problems.append((path, "must be an object {id, text}"))
                continue
            option_id = option.get("id")
            if not is_text(option_id):
                problems.append((path + ".id", "must be a non-empty string"))
            elif option_id in ids:
                problems.append((path + ".id", f'duplicate option id "{option_id}"'))
            else:
                ids.append(option_id)
            if not isinstance(option.get("text"), str):
                problems.append((path + ".text", "must be a string"))
    if kind == "quiz" and choices.get("correct") not in ids:
        problems.append((".correct", "a quiz needs correct set to one of the option ids"))
    elif kind == "poll" and "correct" in choices:
        problems.append((".correct", "a poll must not carry correct"))
    return problems


def _check_figure(figure, path, rep):
    if not isinstance(figure, dict):
        rep.error(path, "must be an object with svg or dataUri")
        return
    if ("svg" in figure) == ("dataUri" in figure):
        rep.error(path, "needs exactly one of svg or dataUri")
    size = 0
    if "svg" in figure:
        svg = figure["svg"]
        if not isinstance(svg, str):
            rep.error(path + ".svg", "must be a string")
        else:
            size += len(svg.encode("utf-8", "replace"))
            if not svg.lstrip().startswith("<svg"):
                rep.error(path + ".svg", "must start with <svg")
            if "<script" in svg.lower():
                rep.error(path + ".svg", "must not contain <script")
    if "dataUri" in figure:
        uri = figure["dataUri"]
        if not isinstance(uri, str) or not uri.startswith("data:image/"):
            rep.error(path + ".dataUri", "must be a data:image/... URI")
        else:
            size += len(uri.encode("utf-8", "replace"))
    for key in ("caption", "alt"):
        if key in figure and not isinstance(figure[key], str):
            rep.error(f"{path}.{key}", "must be a string")
    if size > MAX_FIGURE_BYTES:
        rep.warn(path, f"the figure is {size // 1024} KB; keep one figure under about "
                       f"{MAX_FIGURE_BYTES // 1024} KB")


def _check_links(links, path, rep, ctx):
    if not isinstance(links, list):
        rep.error(path, "must be a list of {to, label, needs}")
        return
    for index, link in enumerate(links):
        link_path = f"{path}[{index}]"
        if not isinstance(link, dict):
            rep.error(link_path, "must be an object {to, label, needs}")
            continue
        target = link.get("to")
        if not is_text(target):
            rep.error(link_path + ".to", "is required: the id of another card")
        else:
            ctx.links.append((link_path + ".to", target))
        if "label" in link and not isinstance(link["label"], str):
            rep.error(link_path + ".label", "must be a string")
        if "needs" in link and not isinstance(link["needs"], bool):
            rep.error(link_path + ".needs", "must be true or false")


def _check_questions(questions, rep, ctx):
    if not isinstance(questions, list):
        rep.error("$.questions", "must be a list")
        return
    for index, question in enumerate(questions):
        path = f"$.questions[{index}]"
        if not isinstance(question, dict):
            rep.error(path, "a question must be an object")
            continue
        ctx.counts["questions"] += 1
        _check_card_id(question, path, rep, ctx)
        if not is_text(question.get("question")):
            rep.error(path + ".question", "is required and must not be empty")
        if "detail" in question and not isinstance(question["detail"], str):
            rep.error(path + ".detail", "must be a string")
        if "lang" in question and not isinstance(question["lang"], str):
            rep.error(path + ".lang", "must be a string such as he or en")
        if "about" in question:
            if is_text(question["about"]):
                ctx.abouts.append((path + ".about", question["about"]))
            else:
                rep.error(path + ".about", "must be the id of a card")
        options = question.get("options")
        ids = []
        if not isinstance(options, list) or len(options) < 2:
            rep.error(path + ".options", "needs a list of at least 2 options {id, label}")
        if isinstance(options, list):
            for position, option in enumerate(options):
                option_path = f"{path}.options[{position}]"
                if not isinstance(option, dict):
                    rep.error(option_path, "must be an object {id, label}")
                    continue
                option_id = option.get("id")
                if not is_text(option_id):
                    rep.error(option_path + ".id", "must be a non-empty string")
                elif option_id in ids:
                    rep.error(option_path + ".id", f'duplicate option id "{option_id}"')
                else:
                    ids.append(option_id)
                if not is_text(option.get("label")):
                    rep.error(option_path + ".label", "must be a non-empty string")
                if "detail" in option and not isinstance(option["detail"], str):
                    rep.error(option_path + ".detail", "must be a string")
        if question.get("recommended") not in ids:
            rep.error(path + ".recommended", "must be one of the option ids")
        _unknown_fields(question, QUESTION_FIELDS, path, rep)


def _dash_owners(plan):
    """The owning card id of every text that holds an em dash or en dash, in page order."""
    owners = []

    def scan(value, owner):
        if isinstance(value, str):
            if has_dash(value):
                owners.append(owner)
        elif isinstance(value, dict):
            for entry in value.values():
                scan(entry, owner)
        elif isinstance(value, list):
            for entry in value:
                scan(entry, owner)

    for key, value in plan.items():
        if key not in ("groups", "questions"):
            scan(value, "plan")
    for kind, card, _depth, _parent, _path in walk_cards(plan.get("groups")):
        owner = str(card.get("id"))
        for key, value in card.items():
            if kind == "group" and key in ("items", "groups"):
                continue
            scan(value, owner)
    questions = plan.get("questions")
    if isinstance(questions, list):
        for question in questions:
            if isinstance(question, dict):
                scan(question, str(question.get("id")))
    return owners


def print_report(rep, warnings=True):
    for path, message in rep.errors:
        note(f"ERROR {path}: {message}")
    if warnings:
        for path, message in rep.warnings:
            note(f"WARNING {path}: {message}")


def load_plan_text(path):
    """(parsed plan, size of the file in bytes)."""
    raw = read_text(path, "plan file")
    try:
        plan = json.loads(raw)
    except ValueError as exc:
        raise CprError(f"the plan file is not valid JSON: {path} ({exc})")
    return plan, len(raw.encode("utf-8", "replace"))


def checked_plan(path, allow_pending, show_warnings=False):
    """Load and validate a plan; on errors print them like validate does and stop."""
    plan, size = load_plan_text(path)
    rep, counts = validate_plan(plan, allow_pending, size)
    if rep.errors:
        print_report(rep, warnings=show_warnings)
        raise CprError(f"the plan is not valid ({plural(len(rep.errors), 'error')}); fix it and "
                       "run again")
    if show_warnings:
        print_report(rep)
    return plan, counts


def cmd_validate(args):
    counts = {}
    try:
        plan, size = load_plan_text(args.plan)
    except CprError as exc:
        rep = Report()
        rep.error("$", exc.message)
    else:
        rep, counts = validate_plan(plan, args.allow_pending, size)
    print_report(rep)
    if args.json:
        emit({"ok": not rep.errors,
              "errors": [f"{path}: {message}" for path, message in rep.errors],
              "warnings": [f"{path}: {message}" for path, message in rep.warnings],
              "counts": counts})
    if rep.errors:
        return EXIT_INVALID
    note(f"OK: {plural(counts['items'], 'item')} in {plural(counts['groups'], 'group')}, "
         f"{plural(counts['questions'], 'question')}")
    return EXIT_OK


# ---------------------------------------------------------------- build

def script_safe_json(plan):
    """Compact JSON that is safe inside <script type="application/json">. The characters that
    could end the block or confuse a parser become JSON escapes, so JSON.parse returns the
    original text."""
    text = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    for char, escape in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                         ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(char, escape)
    return text


def default_template_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, DEFAULT_TEMPLATE))


def render_page(template, plan, standalone=False):
    for token in (PLAN_TOKEN, TITLE_TOKEN):
        found = template.count(token)
        if found != 1:
            raise CprError(f"the page template must contain {token} exactly once (found {found})")
    values = {PLAN_TOKEN: script_safe_json(plan),
              TITLE_TOKEN: html.escape(plan["pageTitle"], quote=True)}
    # One pass, so text in the plan that looks like a token is never replaced.
    pattern = re.compile(re.escape(PLAN_TOKEN) + "|" + re.escape(TITLE_TOKEN))
    page = pattern.sub(lambda match: values[match.group(0)], template)
    if standalone:
        page = STANDALONE_PREFIX + page + STANDALONE_SUFFIX
    return page


def cmd_build(args):
    plan, counts = checked_plan(args.plan, allow_pending=False, show_warnings=True)
    template_path = os.path.abspath(args.template) if args.template else default_template_path()
    template = read_text(template_path, "page template", newline="")
    page = render_page(template, plan, args.standalone)
    size = len(page.encode("utf-8", "replace"))
    if size > MAX_PAGE_BYTES:
        raise CprError(f"the page would be {size} bytes, over the limit of {MAX_PAGE_BYTES} "
                       "bytes (15 MB); shrink the figures or split the plan")
    out = os.path.abspath(args.out)
    write_text(out, page)
    # What the agent needs to meet a read-before-publish rule cheaply: the page is this exact
    # template plus the plan, which sits on one line of its own.
    plan_line = template.count("\n", 0, template.index(PLAN_TOKEN)) + 1
    if args.standalone:
        plan_line += STANDALONE_PREFIX.count("\n")
    emit({"out": out, "bytes": size, "lines": page.count("\n") + 1, "planLine": plan_line,
          "templateSha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
          "pageTitle": plan["pageTitle"], "planId": plan["id"],
          "round": plan["round"], "items": counts["items"], "groups": counts["groups"],
          "questions": counts["questions"], "standalone": bool(args.standalone),
          "capabilities": CAPABILITIES, "icon": ICON})
    return EXIT_OK


# ---------------------------------------------------------------- review.json and published

def load_review(paths, plan_id=None):
    """review.json beside the plan, or None when there is none yet."""
    if not os.path.exists(paths.review):
        return None
    review = read_json_file(paths.review, "review.json")
    if not isinstance(review, dict):
        raise CprError(f"review.json is not a JSON object: {paths.review}")
    if not isinstance(review.get("rounds"), list):
        review["rounds"] = []
    stored = review.get("planId")
    if plan_id is not None and stored is not None and stored != plan_id:
        raise CprError(f"{paths.review} belongs to plan {stored}, not {plan_id}. One review "
                       "folder holds one plan: give the other plan its own folder.")
    return review


def new_review(plan_id):
    return {"planId": plan_id, "url": None, "rounds": []}


def round_entry(review, number):
    """The entry of one round in review.json, created when missing."""
    for entry in review["rounds"]:
        if isinstance(entry, dict) and entry.get("round") == number:
            return entry
    entry = {"round": number}
    review["rounds"].append(entry)
    review["rounds"].sort(
        key=lambda e: e["round"] if isinstance(e, dict) and is_int(e.get("round")) else 0)
    return entry


def cmd_published(args):
    paths = Paths(args.plan)
    plan, _counts = checked_plan(args.plan, allow_pending=False)
    url = (args.url or "").strip()
    if not url:
        raise CprError("--url must be the artifact URL of the published page")
    if not ARTIFACT_URL_RE.match(url):
        note(f"WARNING: {url} does not look like a claude.ai artifact link "
             "(https://claude.ai/artifact/<id> or https://claude.ai/code/artifact/<id>); it is "
             "stored as given. Pass the URL the publish result printed.")
    review = load_review(paths, plan["id"]) or new_review(plan["id"])
    old = review.get("url")
    if old and old != url:
        previous = review.get("previousUrls")
        previous = [entry for entry in previous if entry != url] if isinstance(previous, list) else []
        if old not in previous:
            previous.append(old)
        review["previousUrls"] = previous
        note(f"WARNING: this plan was published at {old} before. One plan should keep one page: "
             "publish later rounds to the same URL. The new URL is stored; the old one is kept "
             "under previousUrls.")
    review["planId"] = plan["id"]
    review["url"] = url
    description = (getattr(args, "description", None) or "").strip()
    if description:
        review["description"] = description
    round_entry(review, plan["round"])["publishedAt"] = now_iso()
    write_json(paths.round_file(plan["round"], "plan.json"), plan)
    write_json(paths.review, review)
    emit(review)
    return EXIT_OK


# ---------------------------------------------------------------- locating the review state

def unwrap_state(data, depth=0):
    """The first object whose schema is the review state, looking up to 3 levels down. A level
    may also be a JSON document stored as a string."""
    if isinstance(data, str):
        text = data.strip()
        if not text.startswith("{"):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
    if isinstance(data, dict):
        if data.get("schema") == STATE_SCHEMA:
            return data
        if depth >= 3:
            return None
        keys = [key for key in WRAP_KEYS if key in data]
        keys += [key for key in data if key not in WRAP_KEYS]
        for key in keys:
            found = unwrap_state(data[key], depth + 1)
            if found is not None:
                return found
    elif isinstance(data, list) and depth < 3:
        for entry in data:
            found = unwrap_state(entry, depth + 1)
            if found is not None:
                return found
    return None


def _state_from_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return unwrap_state(data)


def _state_matches(state, plan):
    number = state.get("round")
    return (state.get("planId") == plan["id"] and is_int(number)
            and number in (plan["round"], plan["round"] - 1))


def _describe_state(path, state):
    return f"{path} (plan {state.get('planId')}, round {state.get('round')})"


def find_state(state_path, plan, paths):
    """(state, absolute path of the file it came from). Stops when nothing usable is found."""
    if os.path.isdir(state_path):
        return _find_state_in_dir(state_path, plan, paths)
    if not os.path.exists(state_path):
        raise CprError(f"--state not found: {state_path}")
    state = unwrap_state(read_json_file(state_path, "review state file"))
    if state is None:
        raise CprError(f"no {STATE_SCHEMA} document in {state_path}: it is neither the saved "
                       "review nor an export of it (looked up to 3 levels deep, under keys such "
                       "as data, doc, document, fields, value)")
    return state, os.path.abspath(state_path)


def _find_state_in_dir(directory, plan, paths):
    others = []
    numbers = [plan["round"]] + ([plan["round"] - 1] if plan["round"] > 1 else [])
    for number in numbers:
        for relative in (os.path.join("review", f"r{number}.json"), f"r{number}.json"):
            candidate = os.path.join(directory, relative)
            if not os.path.isfile(candidate):
                continue
            state = _state_from_file(candidate)
            if state is not None and _state_matches(state, plan):
                return state, os.path.abspath(candidate)
            if state is not None:
                others.append(_describe_state(candidate, state))
    found = []
    skip = os.path.abspath(paths.rounds)
    scanned = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".")
                         and name != "node_modules"
                         and os.path.abspath(os.path.join(root, name)) != skip)
        for name in sorted(files):
            if not name.lower().endswith(".json") or scanned >= MAX_SCAN_FILES:
                continue
            scanned += 1
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_PAGE_BYTES:
                    continue
            except OSError:
                continue
            state = _state_from_file(full)
            if state is None:
                continue
            if _state_matches(state, plan):
                found.append((state, os.path.abspath(full)))
            elif _describe_state(full, state) not in others:
                others.append(_describe_state(full, state))
    if not found:
        detail = (" Found only: " + "; ".join(others[:5]) + ".") if others else ""
        raise CprError(f"no review state for plan {plan['id']}, round {plan['round']}, in "
                       f"{directory}.{detail} Read the saved review with ArtifactData "
                       f"(collection review, doc_id r{plan['round']}), or ask the owner for the "
                       "page's Download or Copy result.")

    def rank(entry):
        state = entry[0]
        updated = state.get("updatedAt")
        return (state.get("round") == plan["round"], updated if isinstance(updated, str) else "")

    best = max(found, key=rank)
    if len(found) > 1:
        note(f"Found {len(found)} review states in {directory}; using {best[1]} (the current "
             "round, latest updatedAt).")
    return best


# ---------------------------------------------------------------- apply

def _mapping(state, key):
    value = state.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CprError(f"the review state is malformed: {key} must be an object")
    return value


def _lock(card, action):
    """The reason a card is locked against an action, or None."""
    locks = card.get("locks") if isinstance(card, dict) else None
    if isinstance(locks, dict) and action in locks:
        reason = locks[action]
        return reason if is_text(reason) else "no reason given"
    return None


def _card_texts(card, kind):
    """Comparable named texts of a card: title, body or summary, field:<key>, choices."""
    texts = {"title": text_of(card.get("title"))}
    if kind == "group":
        texts["summary"] = text_of(card.get("summary"))
        return texts
    texts["body"] = text_of(card.get("body"))
    fields = card.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, dict) and isinstance(field.get("key"), str):
                texts["field:" + field["key"]] = text_of(field.get("value"))
    if card.get("choices") is not None:
        texts["choices"] = json.dumps(card["choices"], ensure_ascii=False, sort_keys=True)
    return texts


def _edited_texts(texts, edits):
    """The same texts with the owner's edits laid over them."""
    result = dict(texts)
    if not isinstance(edits, dict):
        return result
    for key in ("title", "body", "summary"):
        if key in result and isinstance(edits.get(key), str):
            result[key] = edits[key]
    fields = edits.get("fields")
    if isinstance(fields, dict):
        for key, value in fields.items():
            if "field:" + key in result and isinstance(value, str):
                result["field:" + key] = value
    if edits.get("choices") is not None:
        result["choices"] = json.dumps(edits["choices"], ensure_ascii=False, sort_keys=True)
    return result


class Applier(object):
    """Applies one round of the owner's review to a copy of the plan (review-state.md, 5)."""

    def __init__(self, plan, state):
        self.plan = copy.deepcopy(plan)
        self.state = state
        self.number = plan["round"]
        self.warnings = []
        self.nodes = _mapping(state, "nodes")
        self.added_raw = _mapping(state, "added")
        self.order = _mapping(state, "order")
        self.group_order = _mapping(state, "groupOrder")
        self.answers = _mapping(state, "answers")
        instruction = state.get("planInstruction")
        self.plan_instruction = instruction.strip() if isinstance(instruction, str) else ""
        self.ask = []
        self._index()

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def run(self):
        self._read_marks()
        self._read_added()
        self._build_added_cards()
        self._place_groups()
        self._place_items()
        self._find_removed()
        self._apply_marks()
        self._fill_copies()
        self._assemble()
        self._clean_links()
        self._decide_questions()
        self.follow_ups = self._follow_ups()
        if self.plan_instruction or self.follow_ups:
            self.plan["pending"] = {"instruction": self.plan_instruction}
            if self.follow_ups:
                self.plan["pending"]["followUps"] = self.follow_ups
        if "approved" in self.plan:
            del self.plan["approved"]
            self.warn("The plan had been approved before this round; that approval was cleared. "
                      "Run approve again once the rework is done.")
        self.plan["round"] = self.number + 1
        return self.plan

    # -- the plan as it was published

    def _index(self):
        self.groups = {}
        self.items = {}
        self.home = {}
        self.child_groups = {None: []}
        self.child_items = {}
        self.group_seq = []
        self.item_seq = []
        self.page_order = []
        self.original_texts = {}
        for kind, card, _depth, parent, _path in walk_cards(self.plan["groups"]):
            card_id = card["id"]
            parent_id = parent["id"] if parent is not None else None
            if kind == "group":
                self.groups[card_id] = card
                self.group_seq.append(card_id)
                self.child_groups.setdefault(parent_id, []).append(card_id)
                self.child_groups.setdefault(card_id, [])
                self.child_items.setdefault(card_id, [])
            else:
                self.items[card_id] = card
                self.item_seq.append(card_id)
                self.child_items[parent_id].append(card_id)
            self.home[card_id] = parent_id
            self.page_order.append(card_id)
            self.original_texts[card_id] = _card_texts(card, kind)
        questions = self.plan.get("questions")
        self.questions = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
        self.question_ids = set(question["id"] for question in self.questions)
        self.tabs = any(group.get("display") == "tab" for group in self.plan["groups"])

    def _card(self, card_id):
        if card_id in self.groups:
            return self.groups[card_id]
        if card_id in self.items:
            return self.items[card_id]
        return self.new_cards.get(card_id)

    def _is_group(self, card_id):
        return card_id in self.groups or self.new_type.get(card_id) == "group"

    def _title(self, card_id):
        return text_of(self._card(card_id).get("title"))

    def _parent(self, card_id):
        if self._is_group(card_id):
            return self.group_parent.get(card_id)
        return self.item_parent.get(card_id)

    # -- marks on plan cards

    def _read_marks(self):
        self.marks = {}
        for card_id, mark in self.nodes.items():
            if card_id not in self.groups and card_id not in self.items:
                if card_id in self.added_raw:
                    self.warn(f"{card_id}: a mark on a card added in this round was ignored; "
                              "added cards carry their own instruction")
                else:
                    self.warn(f"{card_id}: the review marks a card that is not in this plan; "
                              "ignored")
                continue
            if not isinstance(mark, dict):
                self.warn(f"{card_id}: a malformed mark was ignored")
                continue
            status = mark.get("status")
            if status not in (None, "change", "remove"):
                self.warn(f"{card_id}: unknown status {status!r} treated as Accept")
                status = None
            instruction = mark.get("instruction")
            edits = mark.get("edits")
            if edits is not None and not isinstance(edits, dict):
                self.warn(f"{card_id}: malformed edits were ignored")
                edits = None
            self.marks[card_id] = {
                "status": status,
                "instruction": instruction.strip() if isinstance(instruction, str) else "",
                "edits": edits or {},
            }

    # -- cards the owner added

    def _read_added(self):
        self.new_cards = {}
        self.new_type = {}
        self.new_parent = {}
        self.new_specs = {}
        self.new_seq = []
        self.dropped = set()
        candidates = []
        for card_id, spec in self.added_raw.items():
            kind = spec.get("type") if isinstance(spec, dict) else None
            if kind not in ("item", "group"):
                self.warn(f"{card_id}: an added card without a valid type (item or group) was "
                          "dropped")
            elif not CARD_ID_RE.fullmatch(card_id):
                self.warn(f"{card_id}: an added card with an invalid id was dropped")
            elif card_id in self.groups or card_id in self.items or card_id in self.question_ids:
                self.warn(f"{card_id}: an added card reuses an id that is already in the plan; "
                          "dropped")
            else:
                parent = spec.get("parent")
                if parent == "root" and "root" not in self.groups:
                    parent = None
                candidates.append((card_id, kind, parent, spec))
                continue
            self.dropped.add(card_id)
        accepted = set()
        progress = True
        while progress:
            progress = False
            for card_id, kind, parent, _spec in candidates:
                if kind != "group" or card_id in accepted:
                    continue
                if parent is None or (isinstance(parent, str)
                                      and (parent in self.groups or parent in accepted)):
                    accepted.add(card_id)
                    progress = True
        for card_id, kind, parent, spec in candidates:
            if kind == "group":
                usable = card_id in accepted
            else:
                usable = isinstance(parent, str) and (parent in self.groups or parent in accepted)
            if not usable:
                where = "no parent group" if parent is None else f"parent {parent!r}"
                self.warn(f"{card_id}: an added {kind} has {where}, which does not exist; dropped")
                self.dropped.add(card_id)
                continue
            self.new_type[card_id] = kind
            self.new_parent[card_id] = parent
            self.new_specs[card_id] = spec
            self.new_seq.append(card_id)

    def _build_added_cards(self):
        for card_id in self.new_seq:
            spec = self.new_specs[card_id]
            kind = self.new_type[card_id]
            parent = self.new_parent[card_id]
            instruction = spec.get("instruction")
            instruction = instruction.strip() if isinstance(instruction, str) else ""
            title = text_of(spec.get("title"))
            if kind == "group":
                card = {"id": card_id,
                        "display": "tab" if parent is None and self.tabs else "panel",
                        "kind": spec["kind"] if is_text(spec.get("kind")) else "section",
                        "title": title, "summary": text_of(spec.get("summary")),
                        "items": [], "state": "new"}
                second = card["summary"]
            else:
                card = {"id": card_id, "kind": self._added_kind(card_id, spec, parent),
                        "title": title, "body": text_of(spec.get("body"))}
                fields = self._added_fields(card_id, spec)
                if fields is not None:
                    card["fields"] = fields
                if spec.get("choices") is not None:
                    problems = choices_problems(spec["choices"])
                    if problems:
                        self.warn(f"{card_id}: the added card's choices are not well-formed "
                                  f"(choices{problems[0][0]}: {problems[0][1]}); left out")
                    else:
                        card["choices"] = spec["choices"]
                if is_text(spec.get("copyOf")):
                    card["copyOf"] = spec["copyOf"]
                card["state"] = "new"
                second = card["body"]
            if instruction or not (title.strip() or second.strip()):
                card["pending"] = {"instruction": instruction, "draft": True}
                if not instruction:
                    self.ask.append(card_id)
                    self.warn(f"{card_id}: added with no text and no instruction; ask the owner "
                              "what it should hold")
            else:
                names = self._owner_names(card, kind, spec.get("copyOf"))
                if names:
                    card["ownerEdited"] = names
            self.new_cards[card_id] = card

    def _added_kind(self, card_id, spec, parent):
        kind = spec.get("kind")
        if isinstance(kind, str) and KIND_RE.fullmatch(kind):
            return kind
        fallback = "item"
        group = self.groups.get(parent)
        if group is not None:
            add_kind = group.get("addKind")
            kinds = [self.items[item_id].get("kind") for item_id in self.child_items[parent]]
            if isinstance(add_kind, str) and KIND_RE.fullmatch(add_kind):
                fallback = add_kind
            elif kinds:
                fallback = kinds[-1]
        self.warn(f"{card_id}: an added item had no valid kind; {fallback} was used")
        return fallback

    def _added_fields(self, card_id, spec):
        fields = spec.get("fields")
        if fields is None:
            return None
        if isinstance(fields, dict):
            labels = {}
            source = self._card(spec.get("copyOf")) if isinstance(spec.get("copyOf"), str) else None
            for field in (source or {}).get("fields") or []:
                if isinstance(field, dict) and isinstance(field.get("key"), str):
                    labels[field["key"]] = text_of(field.get("label")) or field["key"]
            fields = [{"key": key, "label": labels.get(key, key), "value": value}
                      for key, value in fields.items()]
        problems = fields_problems(fields)
        if problems:
            self.warn(f"{card_id}: the added card's fields are not well-formed "
                      f"(fields{problems[0][0]}: {problems[0][1]}); left out")
            return None
        return fields

    def _source_versions(self, copy_of):
        """The texts a duplicate may have copied: its source before and after this round's
        edits, or None when copy_of names no known card."""
        if not isinstance(copy_of, str):
            return None
        if copy_of in self.original_texts:
            original = self.original_texts[copy_of]
            edits = self.marks.get(copy_of, {}).get("edits")
            return [original, _edited_texts(original, edits)]
        if copy_of in self.new_specs:
            return [_card_texts(self.new_specs[copy_of], self.new_type[copy_of])]
        return None

    def _owner_names(self, card, kind, copy_of):
        """Names of the added card's texts that are the owner's own words."""
        texts = _card_texts(card, kind)
        names = [name for name, value in texts.items() if value.strip()]
        versions = self._source_versions(copy_of)
        if copy_of is not None and versions is None:
            self.warn(f"{card['id']}: copyOf names {copy_of!r}, which is not in the plan; all of "
                      "its text is treated as the owner's")
        if versions:
            names = [name for name in names
                     if all(version.get(name) != texts[name] for version in versions)]
        return names

    # -- the new structure

    def _place_groups(self):
        """Final sub-group lists per parent (None: the top level), from groupOrder."""
        added_groups = [card_id for card_id in self.new_seq if self.new_type[card_id] == "group"]
        all_groups = self.group_seq + added_groups
        known = set(all_groups)
        for key in self.group_order:
            if key != "root" and key not in known:
                self.warn(f"groupOrder names {key!r}, which is not a group in this plan; ignored")
        claims = {}
        explicit = {}
        for key in ["root"] + [group_id for group_id in all_groups if group_id != "root"]:
            listed = self.group_order.get(key)
            if listed is None:
                continue
            if not isinstance(listed, list):
                self.warn(f"groupOrder for {key} is not a list; ignored")
                continue
            parent = None if key == "root" else key
            sequence = []
            for group_id in listed:
                if not isinstance(group_id, str) or group_id not in known:
                    if not (isinstance(group_id, str) and group_id in self.dropped):
                        self.warn(f"groupOrder for {key} lists {group_id!r}, which is not a group "
                                  "in this plan; ignored")
                elif group_id in claims:
                    self.warn(f"groupOrder lists {group_id} more than once; it keeps its first "
                              "place")
                elif group_id == parent:
                    self.warn(f"groupOrder puts {group_id} inside itself; ignored")
                else:
                    claims[group_id] = parent
                    sequence.append(group_id)
            explicit[parent] = sequence
        fallback = {group_id: self.home[group_id] for group_id in self.group_seq}
        for group_id in added_groups:
            fallback[group_id] = self.new_parent[group_id]
        parent_of = {group_id: claims[group_id] if group_id in claims else fallback[group_id]
                     for group_id in all_groups}
        reset = set()
        for _attempt in range(len(all_groups) + 1):
            broken = [group_id for group_id in all_groups if not _reaches_top(group_id, parent_of)]
            if not broken:
                break
            for group_id in broken:
                if group_id in claims:
                    del claims[group_id]
                    reset.add(group_id)
                    parent_of[group_id] = fallback[group_id]
                    self.warn(f"{group_id}: the new order would put this group inside itself; it "
                              "stays where it was")
                elif _attempt == len(all_groups):
                    parent_of[group_id] = None
        self.final_groups = {}
        for parent in [None] + all_groups:
            sequence = [group_id for group_id in explicit.get(parent, [])
                        if group_id in claims and claims[group_id] == parent]
            for group_id in self.child_groups.get(parent, []):
                if group_id not in claims and parent_of[group_id] == parent:
                    sequence.append(group_id)
                    if parent in explicit and group_id not in reset:
                        self.warn(f"{group_id}: not listed in the new order of "
                                  f"{parent or 'the top level'}; kept at its end")
            for group_id in added_groups:
                if (group_id not in claims and parent_of[group_id] == parent
                        and group_id not in sequence):
                    sequence.append(group_id)
            self.final_groups[parent] = sequence
        self.group_parent = parent_of

    def _place_items(self):
        """Final item lists per group, from order."""
        all_groups = list(self.group_parent)
        known_groups = set(all_groups)
        all_items = set(self.item_seq)
        all_items.update(card_id for card_id in self.new_seq if self.new_type[card_id] == "item")
        for key in self.order:
            if key not in known_groups:
                self.warn(f"order names {key!r}, which is not a group in this plan; ignored")
        claims = {}
        explicit = {}
        for group_id in all_groups:
            listed = self.order.get(group_id)
            if listed is None:
                continue
            if not isinstance(listed, list):
                self.warn(f"order for {group_id} is not a list; ignored")
                continue
            sequence = []
            for item_id in listed:
                if not isinstance(item_id, str) or item_id not in all_items:
                    if not (isinstance(item_id, str) and item_id in self.dropped):
                        self.warn(f"order for {group_id} lists {item_id!r}, which is not an item "
                                  "in this plan; ignored")
                elif item_id in claims:
                    self.warn(f"order lists {item_id} more than once; it keeps its first place")
                else:
                    claims[item_id] = group_id
                    sequence.append(item_id)
            explicit[group_id] = sequence
        self.final_items = {}
        self.item_parent = {}
        for group_id in all_groups:
            sequence = list(explicit.get(group_id, []))
            for item_id in self.child_items.get(group_id, []):
                if item_id not in claims:
                    sequence.append(item_id)
                    if group_id in explicit:
                        self.warn(f"{item_id}: not listed in the new order of {group_id}; kept "
                                  "at its end")
            for card_id in self.new_seq:
                if (self.new_type[card_id] == "item" and self.new_parent[card_id] == group_id
                        and card_id not in claims):
                    sequence.append(card_id)
            self.final_items[group_id] = sequence
            for item_id in sequence:
                self.item_parent[item_id] = group_id

    def _inside(self, group_id):
        """Every card that ends up inside a group, in page order."""
        found = list(self.final_items.get(group_id, []))
        for sub in self.final_groups.get(group_id, []):
            found.append(sub)
            found.extend(self._inside(sub))
        return found

    # -- removal

    def _find_removed(self):
        self.removed = set()
        self.removed_entries = []
        marked = []
        for card_id in self.page_order:
            if self.marks.get(card_id, {}).get("status") != "remove":
                continue
            reason = _lock(self._card(card_id), "remove")
            if reason is not None:
                self.warn(f"{card_id}: marked Remove but locked against removal ({reason}); kept. "
                          "Tell the owner.")
            else:
                marked.append(card_id)
        removable = set()
        for card_id in marked:
            if card_id in self.groups:
                locked = [inner for inner in self._inside(card_id)
                          if _lock(self._card(inner), "remove") is not None]
                if locked:
                    self.warn(f"{card_id}: marked Remove but it holds {', '.join(locked)}, which "
                              "is locked against removal; the group was kept. Ask the owner.")
                    continue
            removable.add(card_id)
        for card_id in marked:
            if card_id not in removable or card_id in self.removed:
                continue
            entry = {"id": card_id, "type": "group" if card_id in self.groups else "item",
                     "title": self._title(card_id)}
            if self.marks[card_id]["instruction"]:
                entry["instruction"] = self.marks[card_id]["instruction"]
            self.removed.add(card_id)
            if card_id in self.groups:
                inside = self._inside(card_id)
                self.removed.update(inside)
                if inside:
                    entry["inside"] = inside
                self._report_ignored_inside(card_id, inside)
            self.removed_entries.append(entry)
        # a removed card inside a group that was removed later in page order is covered by it
        self.removed_entries = [entry for entry in self.removed_entries
                                if not any(entry["id"] in other.get("inside", [])
                                           for other in self.removed_entries)]

    def _report_ignored_inside(self, group_id, inside):
        notes = []
        for card_id in inside:
            if card_id in self.new_type:
                notes.append(f"{card_id} (added)")
                continue
            mark = self.marks.get(card_id)
            if mark is None:
                continue
            parts = [part for part, present in ((mark["status"], bool(mark["status"])),
                                                ("instruction", bool(mark["instruction"])),
                                                ("edits", bool(mark["edits"]))) if present]
            if parts:
                notes.append(f"{card_id} ({', '.join(parts)})")
        if notes:
            self.warn(f"{group_id} was removed with everything in it; the marks inside it were "
                      f"ignored: {'; '.join(notes)}")

    # -- marks on surviving plan cards

    def _apply_marks(self):
        self.edited = []
        self.changes = []
        self.ignored = []
        for card_id in self.page_order:
            if card_id in self.removed:
                continue
            card = self._card(card_id)
            card.pop("state", None)
            card.pop("agentNote", None)
            if self._parent(card_id) != self.home[card_id]:
                reason = _lock(card, "move")
                if reason is not None:
                    where = self._parent(card_id) or "the top level"
                    self.warn(f"{card_id}: locked against moving ({reason}), but the review moved "
                              f"it to {where}; check with the owner")
            mark = self.marks.get(card_id)
            if mark is None:
                continue
            kind = "group" if card_id in self.groups else "item"
            if mark["edits"]:
                names = self._apply_edits(card_id, kind, card, mark["edits"])
                if names:
                    self.edited.append((card_id, names))
            if mark["status"] == "change":
                card["pending"] = {"instruction": mark["instruction"]}
                self.changes.append(card_id)
                if not mark["instruction"]:
                    self.ask.append(card_id)
                    self.warn(f"{card_id}: marked Change with no instruction; ask the owner what "
                              "to change")
            elif mark["status"] is None and mark["instruction"]:
                self.ignored.append((card_id, mark["instruction"]))

    def _apply_edits(self, card_id, kind, card, edits):
        reason = _lock(card, "edit")
        if reason is not None:
            self.warn(f"{card_id}: locked against editing ({reason}); the owner's edits "
                      f"({', '.join(edits)}) were not applied. Ask the owner.")
            return []
        names = []
        text_keys = ("title", "summary") if kind == "group" else ("title", "body")
        for key, value in edits.items():
            if key in text_keys:
                if not isinstance(value, str):
                    self.warn(f"{card_id}: the edit of {key} is not text; ignored")
                elif value != card.get(key):
                    card[key] = value
                    names.append(key)
                    if key == "title" and not value.strip():
                        self.warn(f"{card_id}: the owner cleared the title; write one or ask")
            elif key == "fields" and kind == "item":
                names.extend(self._apply_field_edits(card_id, card, value))
            elif key == "choices" and kind == "item":
                problems = choices_problems(value)
                if problems:
                    self.warn(f"{card_id}: the owner's choices edit is not well-formed "
                              f"(choices{problems[0][0]}: {problems[0][1]}); the original was "
                              "kept")
                elif value != card.get("choices"):
                    card["choices"] = value
                    names.append("choices")
            else:
                self.warn(f"{card_id}: unknown edit {key!r} ignored")
        if names:
            existing = card.get("ownerEdited") if is_str_list(card.get("ownerEdited")) else []
            card["ownerEdited"] = existing + [name for name in names if name not in existing]
        return names

    def _apply_field_edits(self, card_id, card, values):
        if not isinstance(values, dict):
            self.warn(f"{card_id}: the field edits are malformed; ignored")
            return []
        fields = card.get("fields") if isinstance(card.get("fields"), list) else []
        by_key = {field["key"]: field for field in fields
                  if isinstance(field, dict) and isinstance(field.get("key"), str)}
        names = []
        for key, value in values.items():
            field = by_key.get(key)
            if field is None:
                self.warn(f"{card_id}: the owner edited field {key!r}, which the card does not "
                          "have; ignored")
            elif not isinstance(value, str):
                self.warn(f"{card_id}: the edit of field {key!r} is not text; ignored")
            elif value != field.get("value"):
                field["value"] = value
                names.append("field:" + key)
        return names

    def _fill_copies(self):
        """A duplicate made on the page carries only a title and a body. It takes the rest from
        its source card as that card stands after this round's edits: label, language, fields,
        options and figure."""
        for card_id in self.new_seq:
            if self.new_type[card_id] != "item":
                continue
            card = self.new_cards[card_id]
            source = self.items.get(card.get("copyOf"))
            if source is None:
                continue
            for key in ("label", "lang", "fields", "choices", "figure"):
                if key not in card and key in source:
                    card[key] = copy.deepcopy(source[key])

    def _follow_ups(self):
        """Work that hangs on no single card, one line each: the answers the owner gave to the
        agent's questions, and notes left on removed cards. They make the plan itself pending,
        so nothing is approved before the agent has acted on them."""
        lines = []
        for decision in self.round_decisions:
            if decision["by"] != "owner":
                continue
            answer = one_line(decision["label"]) or str(decision["choice"])
            line = f"Answer to {decision['id']} ({one_line(decision['question'])}): {answer}"
            if decision["note"]:
                line += f". Note: {one_line(decision['note'])}"
            lines.append(line)
        for entry in self.removed_entries:
            if entry.get("instruction"):
                lines.append(f"Note on removed {entry['id']} ({one_line(entry['title'])}): "
                             f"{one_line(entry['instruction'])}")
        return lines

    # -- the rebuilt plan

    def _assemble(self):
        def build(group_id, depth):
            card = self._card(group_id)
            items = [self._card(item_id) for item_id in self.final_items.get(group_id, [])
                     if item_id not in self.removed]
            subgroups = [build(sub, depth + 1) for sub in self.final_groups.get(group_id, [])
                         if sub not in self.removed]
            if depth == 1 and self.tabs:
                card["display"] = "tab"
            elif depth > 1 and card.get("display") == "tab":
                card["display"] = "panel"
            if "items" in card or items:
                card["items"] = items
            if "groups" in card or subgroups:
                card["groups"] = subgroups
            if depth > MAX_DEPTH:
                self.warn(f"{group_id}: now nests {depth} levels deep, and at most {MAX_DEPTH} "
                          "are allowed; flatten it while reworking")
            return card

        self.plan["groups"] = [build(group_id, 1) for group_id in self.final_groups.get(None, [])
                               if group_id not in self.removed]
        if not self.plan["groups"]:
            self.warn("Every group was removed, so the plan has no content left. Ask the owner "
                      "how to continue.")

    def _clean_links(self):
        for card_id in self.page_order + self.new_seq:
            if card_id in self.removed:
                continue
            card = self._card(card_id)
            links = card.get("links")
            if not isinstance(links, list):
                continue
            kept = []
            for link in links:
                target = link.get("to") if isinstance(link, dict) else None
                if isinstance(target, str) and target in self.removed:
                    if link.get("needs") is True:
                        label = f" ({one_line(link['label'])})" if is_text(link.get("label")) else ""
                        self.warn(f"{card_id} needs {target}{label}, which was removed; rework "
                                  f"{card_id} or ask the owner")
                    continue
                kept.append(link)
            card["links"] = kept

    def _decide_questions(self):
        self.round_decisions = []
        self.owner_answers = 0
        for question in self.questions:
            question_id = question["id"]
            options = [option for option in question.get("options") or [] if isinstance(option, dict)]
            labels = {option["id"]: text_of(option.get("label")) for option in options
                      if isinstance(option.get("id"), str)}
            recommended = question.get("recommended")
            answer = self.answers.get(question_id)
            note_text = ""
            if isinstance(answer, dict):
                by = "owner"
                self.owner_answers += 1
                choice = answer.get("choice")
                if isinstance(answer.get("note"), str):
                    note_text = answer["note"].strip()
                if not isinstance(choice, str) or choice not in labels:
                    if choice is not None:
                        self.warn(f"{question_id}: the answer {choice!r} is not one of the "
                                  f"options; the recommended {recommended!r} was used and the "
                                  "note kept")
                    choice = recommended
            else:
                if answer is not None:
                    self.warn(f"{question_id}: a malformed answer was ignored; the recommended "
                              "answer was used")
                by = "default"
                choice = recommended
            self.round_decisions.append({
                "id": question_id, "question": text_of(question.get("question")),
                "choice": choice, "label": labels.get(choice, ""), "note": note_text,
                "by": by, "round": self.number})
        for question_id in self.answers:
            if question_id not in self.question_ids:
                self.warn(f"{question_id}: an answer to a question that is not in the plan was "
                          "ignored")
        earlier = self.plan.get("decisions")
        if self.round_decisions or isinstance(earlier, list):
            self.plan["decisions"] = (earlier if isinstance(earlier, list) else []) + self.round_decisions
        if "questions" in self.plan:
            self.plan["questions"] = []

    # -- what happened, for the summary and the record

    def pending_entries(self):
        entries = []
        for kind, card_id, title, pending in collect_pending(self.plan):
            instruction = pending.get("instruction") if isinstance(pending, dict) else ""
            entry = {"id": card_id, "type": kind, "title": text_of(title),
                     "instruction": text_of(instruction),
                     "draft": isinstance(pending, dict) and pending.get("draft") is True}
            if isinstance(pending, dict) and pending.get("followUps"):
                entry["followUps"] = list(pending["followUps"])
            entries.append(entry)
        return entries

    def movements(self):
        """(number of plan cards that changed parent, record lines for moves and reorders)."""
        moved = 0
        lines = []

        def where(parent):
            return "the top level" if parent is None else f"`{parent}`"

        for card_id in self.page_order:
            if card_id in self.removed or self._parent(card_id) == self.home[card_id]:
                continue
            moved += 1
            lines.append(f"{ref(card_id, self._title(card_id))}: moved from "
                         f"{where(self.home[card_id])} to {where(self._parent(card_id))}")
        for parent in [None] + self.group_seq:
            if parent in self.removed:
                continue
            before = [group_id for group_id in self.child_groups.get(parent, [])
                      if group_id not in self.removed and self.group_parent[group_id] == parent]
            after = [group_id for group_id in self.final_groups.get(parent, []) if group_id in before]
            if before != after:
                lines.append("Top-level groups reordered" if parent is None
                             else f"{ref(parent, self._title(parent))}: sub-groups reordered")
            if parent is None:
                continue
            before = [item_id for item_id in self.child_items.get(parent, [])
                      if item_id not in self.removed and self.item_parent.get(item_id) == parent]
            after = [item_id for item_id in self.final_items.get(parent, []) if item_id in before]
            if before != after:
                lines.append(f"{ref(parent, self._title(parent))}: items reordered")
        return moved, lines

    def summary(self, finished, second_look, dry_run, files):
        pending = self.pending_entries()
        moved, self.move_lines = self.movements()
        owner_edits = [{"id": card_id, "title": self._title(card_id), "fields": names}
                       for card_id, names in self.edited]
        added = [{"id": card["id"], "type": kind, "title": text_of(card.get("title")),
                  "draft": is_draft(card)}
                 for kind, card, _depth, _parent, _path in walk_cards(self.plan["groups"])
                 if card["id"] in self.new_cards]
        ignored = [{"id": card_id, "title": self._title(card_id), "instruction": text}
                   for card_id, text in self.ignored]
        counts = {"change": len(self.changes), "remove": len(self.removed_entries),
                  "added": len(added), "edited": len(owner_edits), "moved": moved,
                  "answers": self.owner_answers}
        ask = [card_id for card_id in self.ask if card_id not in self.removed]
        return {
            "planId": self.plan["id"], "round": self.number, "nextRound": self.number + 1,
            "finished": finished, "alreadyApplied": False, "dryRun": dry_run,
            "secondLookRequested": second_look,
            "needsSecondLook": bool(second_look and pending),
            "counts": counts, "planInstruction": self.plan_instruction, "pending": pending,
            "ownerEdits": owner_edits, "removed": self.removed_entries, "added": added,
            "decisions": self.round_decisions, "ignoredInstructions": ignored,
            "warnings": list(self.warnings), "files": files,
            "next": next_step(dry_run, len(pending), second_look, self.number, ask),
        }

    def record(self, summary, finish):
        """rounds/round-N.md: the readable record of the round, and the work order."""
        look = "yes" if summary["secondLookRequested"] else "no"
        out = [f"# {one_line(self.plan.get('title'))} - review round {self.number}", ""]
        if isinstance(finish, dict):
            at = finish.get("at") if is_text(finish.get("at")) else "an unknown time"
            out.append(f"Finished on the page at {at}. Second look requested: {look}.")
        else:
            out.append("Not finished on the page; applied with --allow-unfinished. Second look "
                       f"requested: {look}.")

        def section(heading, entries):
            if entries:
                out.extend(["", f"## {heading}", ""])
                out.extend(f"- {entry}" for entry in entries)

        def place(card_id):
            parent = self._parent(card_id)
            kind = "group" if self._is_group(card_id) else self._card(card_id).get("kind", "item")
            return f"{kind} in " + ("the top level" if parent is None else f"`{parent}`")

        if summary["planInstruction"]:
            out.extend(["", "## Whole-plan instruction", "", summary["planInstruction"]])
        section("Follow-ups (act on each in the cards it affects, then delete the plan's pending)",
                self.follow_ups)
        rework, draft = [], []
        for entry in summary["pending"]:
            if entry["type"] == "plan":
                continue
            what = one_line(entry["instruction"])
            if entry["draft"]:
                draft.append(f"{ref(entry['id'], entry['title'])} ({place(entry['id'])}): "
                             + (what or "no instruction; ask the owner what it should hold"))
            else:
                kind = " (group)" if entry["type"] == "group" else ""
                rework.append(f"{ref(entry['id'], entry['title'])}{kind}: "
                              + (what or "no instruction; ask the owner what to change"))
        section("To rework", rework)
        section("To draft", draft)
        as_written = []
        for entry in summary["added"]:
            if entry["draft"]:
                continue
            card = self.new_cards[entry["id"]]
            names = card.get("ownerEdited") or []
            copied = f", copy of `{card['copyOf']}`" if card.get("copyOf") else ""
            words = f": the owner's words in {', '.join(names)}" if names else ": unchanged copy"
            as_written.append(f"{ref(entry['id'], entry['title'])} ({place(entry['id'])}{copied})"
                              f"{words}")
        section("Added as written by the owner", as_written)
        section("Owner edits (keep the wording, fix only typos and style rule violations)",
                [f"{ref(entry['id'], entry['title'])}: {', '.join(entry['fields'])}"
                 for entry in summary["ownerEdits"]])
        removed = []
        for entry in summary["removed"]:
            inside = entry.get("inside") or []
            extra = f", with {plural(len(inside), 'card')} inside: {', '.join(inside)}" if inside else ""
            said = f". The owner's note: {one_line(entry['instruction'])}" if entry.get("instruction") else ""
            removed.append(f"{ref(entry['id'], entry['title'])} ({entry['type']}{extra}){said}")
        section("Removed", removed)
        section("Reordered or moved", self.move_lines)
        answers = []
        for decision in summary["decisions"]:
            how = "the owner's answer" if decision["by"] == "owner" else "recommended, untouched"
            line = (f"`{decision['id']}` {one_line(decision['question'])}: "
                    f"{one_line(decision['label']) or decision['choice']} ({how})")
            if decision["note"]:
                line += f". Note: {one_line(decision['note'])}"
            answers.append(line)
        section("Answers", answers)
        section("Instructions not applied",
                [f"{ref(entry['id'], entry['title'])}: {one_line(entry['instruction'])} (the card "
                 "was accepted, so this was not applied; mention it to the owner)"
                 for entry in summary["ignoredInstructions"]])
        section("Warnings", summary["warnings"])
        return "\n".join(out) + "\n"


def _reaches_top(group_id, parent_of):
    seen = set()
    current = group_id
    while current is not None:
        if current in seen:
            return False
        seen.add(current)
        current = parent_of.get(current)
    return True


def next_step(dry_run, pending_count, second_look, number, ask):
    if dry_run:
        return f"Dry run: nothing was written; run apply again without --dry-run to apply round {number}."
    if pending_count == 0:
        return "Nothing waits for rework: run `approve`."
    if second_look:
        tail = f"validate, build and publish round {number + 1} at the same URL."
    else:
        tail = "validate and run `approve` (the owner asked for no second look)."
    rework = (f"rework the {plural(pending_count, 'pending card')} (delete each `pending` when "
              f"done), then {tail}")
    if ask:
        return f"First ask the owner what {', '.join(ask)} should become; then {rework}"
    return rework[0].upper() + rework[1:]


def same_review(stored, state):
    """Whether two review states are the same review: same updatedAt, or the same content when
    neither has one."""
    if stored.get("updatedAt") is not None or state.get("updatedAt") is not None:
        return stored.get("updatedAt") == state.get("updatedAt")

    def strip(value):
        return {key: entry for key, entry in value.items() if key != "exportedAt"}

    return strip(stored) == strip(state)


def second_look_of(finish, plan):
    if isinstance(finish, dict):
        if isinstance(finish.get("secondLook"), bool):
            return finish["secondLook"]
        if isinstance(plan.get("secondLookDefault"), bool):
            return plan["secondLookDefault"]
    return True


def _aside_path(paths, number):
    stem = paths.round_file(number, f"reworked-{file_stamp()}")
    candidate = stem + ".plan.json"
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{stem}-{counter}.plan.json"
        counter += 1
    return candidate


def _round_mismatch(number, plan_round):
    if number == plan_round - 1:
        return (f"the review is for round {number} and the plan is at round {plan_round}, but "
                f"rounds/ holds no applied record of round {number}: the plan was changed by "
                "hand, or this is not the folder the round was applied in")
    if number > plan_round:
        return (f"the review is for round {number}, but the plan is only at round {plan_round}: "
                "was the plan reset, or is this the review of another copy?")
    return (f"the review is for round {number}, but the plan is at round {plan_round}: it is an "
            f"old review; read the review of round {plan_round} instead")


def _already_applied(paths, plan, number):
    """The same review again: report where things stand now, not the old work list."""
    path = paths.round_file(number, "summary.json")
    saved = None
    if os.path.isfile(path):
        try:
            saved = read_json_file(path, "saved summary")
        except CprError:
            saved = None
    if not isinstance(saved, dict):
        saved = {}
    needs = saved.get("needsSecondLook")
    pending_now = len(collect_pending(plan))
    try:
        review = load_review(paths, plan["id"])
    except CprError:
        review = None
    next_round = number + 1
    published_next = any(isinstance(entry, dict) and entry.get("round") == next_round
                         and entry.get("publishedAt")
                         for entry in (review or {}).get("rounds") or [])
    if isinstance(plan.get("approved"), dict):
        step = "The plan is approved; nothing is left to do for this review."
    elif pending_now:
        tail = (f"validate, build and publish round {next_round} at the same URL."
                if needs is not False else "validate and run `approve`.")
        step = (f"{plural(pending_now, 'card')} in plan.json still "
                f"{'owes' if pending_now == 1 else 'owe'} rework; finish it, then {tail}")
    elif published_next:
        step = (f"Round {next_round} is already published; wait for the owner to finish it.")
    elif needs is True:
        step = (f"The rework is done and the owner asked to see it: validate, build and publish "
                f"round {next_round} at the same URL.")
    elif needs is False:
        step = "Nothing waits for a second look: run `approve`."
    else:
        step = (f"The saved summary of round {number} is missing: if anything was reworked or "
                f"drafted and the owner asked to see it, publish round {next_round}; otherwise "
                "run `approve`.")
    note(f"Round {number} was already applied from this same review; the owner has not changed "
         "it since, and nothing was written.")
    emit({"planId": plan["id"], "round": number, "alreadyApplied": True,
          "needsSecondLook": needs, "pendingNow": pending_now,
          "nextRoundPublished": published_next, "appliedSummary": path if saved else None,
          "next": step})
    return EXIT_OK


def cmd_apply(args):
    paths = Paths(args.plan)
    current, _size = load_plan_text(args.plan)
    if not (isinstance(current, dict) and isinstance(current.get("id"), str)
            and is_int(current.get("round"))):
        checked_plan(args.plan, allow_pending=True)
        raise CprError("the plan needs a string id and an integer round")
    review = load_review(paths, current["id"])
    state, _source = find_state(args.state, current, paths)
    if state.get("planId") != current["id"]:
        raise CprError(f"the review is for plan {state.get('planId')!r}, but this plan is "
                       f"{current['id']!r}")
    number = state.get("round")
    if not is_int(number):
        raise CprError("the review state has no valid round")
    aside = None
    if number == current["round"]:
        base = current
    elif number == current["round"] - 1 and os.path.isfile(paths.round_file(number, "state.json")):
        stored = read_json_file(paths.round_file(number, "state.json"), "saved review state")
        if isinstance(stored, dict) and same_review(stored, state):
            if args.reapply:
                note("Note: --reapply is not needed; the review has not changed since it was "
                     "applied.")
            return _already_applied(paths, current, number)
        if not args.reapply:
            then = stored.get("updatedAt") if isinstance(stored, dict) else None
            raise CprError(
                f"round {number} was already applied, and the owner changed the review after "
                f"that (updatedAt {then} when applied, {state.get('updatedAt')} now). Run apply "
                f"again with --reapply to start over from the saved copy of round {number}; the "
                "current plan.json, with any rework done since, is set aside in rounds/ first.",
                EXIT_CHANGED)
        base_path = paths.round_file(number, "plan.json")
        if not os.path.isfile(base_path):
            raise CprError(f"cannot reapply: the saved copy of round {number} is missing "
                           f"({base_path})")
        base = read_json_file(base_path, f"saved copy of round {number}")
        aside = _aside_path(paths, number)
    else:
        raise CprError(_round_mismatch(number, current["round"]))
    rep, _counts = validate_plan(base, allow_pending=True)
    if rep.errors:
        print_report(rep, warnings=False)
        raise CprError("the plan is not valid; fix it and run apply again")
    if base.get("id") != current["id"] or base.get("round") != number:
        raise CprError(f"the saved copy of round {number} is for another plan or round")
    finish = state.get("finish")
    finished = isinstance(finish, dict)
    if not finished and not args.allow_unfinished:
        raise CprError(f"the owner has not pressed Finish review for round {number}. Ask the "
                       "owner whether the review is done; if they said in the conversation that "
                       "it is, run apply again with --allow-unfinished.", EXIT_UNFINISHED)
    applier = Applier(base, state)
    new_plan = applier.run()
    if aside is not None:
        verb = "would be" if args.dry_run else "was"
        applier.warnings.append(f"Earlier rework {verb} set aside in {aside}: the review was "
                                f"applied again from the saved copy of round {number}.")
    files = {"plan": None, "record": None, "state": None, "summary": None}
    if not args.dry_run:
        files = {"plan": paths.plan, "record": paths.round_file(number, "md"),
                 "state": paths.round_file(number, "state.json"),
                 "summary": paths.round_file(number, "summary.json")}
    summary = applier.summary(finished, second_look_of(finish, base), args.dry_run, files)
    record = applier.record(summary, finish)
    if not args.dry_run:
        if aside is not None:
            os.makedirs(paths.rounds, exist_ok=True)
            shutil.copyfile(paths.plan, aside)
        if not os.path.isfile(paths.round_file(number, "plan.json")):
            write_json(paths.round_file(number, "plan.json"), base)
        write_json(files["state"], state)
        write_text(files["record"], record)
        write_json(files["summary"], summary)
        write_json(paths.plan, new_plan)
        review = review or new_review(base["id"])
        entry = round_entry(review, number)
        entry["appliedAt"] = now_iso()
        entry["finishedAt"] = finish.get("at") if finished else None
        entry["stateUpdatedAt"] = state.get("updatedAt")
        write_json(paths.review, review)
    emit(summary)
    note(summary["next"])
    return EXIT_OK


# ---------------------------------------------------------------- approve, render, status

def last_applied_round(paths):
    numbers = set()
    try:
        review = load_review(paths)
    except CprError:
        review = None
    for entry in (review or {}).get("rounds") or []:
        if isinstance(entry, dict) and entry.get("appliedAt") and is_int(entry.get("round")):
            numbers.add(entry["round"])
    if os.path.isdir(paths.rounds):
        for name in os.listdir(paths.rounds):
            match = re.fullmatch(r"round-(\d+)\.state\.json", name)
            if match:
                numbers.add(int(match.group(1)))
    return max(numbers) if numbers else 0


def cmd_approve(args):
    paths = Paths(args.plan)
    plan, _counts = checked_plan(args.plan, allow_pending=True)
    found = collect_pending(plan)
    if found:
        raise CprError(pending_label(found) + "; finish the rework (and publish another round if "
                       "the owner wants a second look) before approving")
    if plan.get("questions"):
        note(f"WARNING: {plural(len(plan['questions']), 'question')} were never answered; the "
             "approved plan keeps them as open questions")
    held = placeholder_owners(plan)
    if held:
        note(f"WARNING: a placeholder is still in {plural(len(held), 'text')} "
             f"({', '.join(held[:5])}); a build usually rejects it, so ask the owner for the value")
    after = last_applied_round(paths)
    at = now_iso()
    plan["approved"] = {"at": at, "afterRound": after}
    out = os.path.abspath(args.out) if args.out else os.path.join(paths.folder, "PLAN.md")
    write_json(paths.plan, plan)
    write_text(out, render_markdown(plan))
    review = load_review(paths, plan["id"]) or new_review(plan["id"])
    review["approvedAt"] = at
    write_json(paths.review, review)
    result = {"approved": True, "planMd": out, "afterRound": after}
    if held:
        result["placeholders"] = held
    emit(result)
    return EXIT_OK


def _pending_mark(pending):
    instruction = pending.get("instruction") if isinstance(pending, dict) else None
    return f"(pending: {one_line(instruction)})" if is_text(instruction) else "(pending)"


def _cell(value):
    return one_line(value).replace("|", "\\|")


def _item_lines(item, number, mode, kinds):
    marker = "-" if mode == "none" else f"{number}."
    indent = " " * (len(marker) + 1)
    kind = text_of(item.get("kind"))
    spec = kinds.get(kind) if isinstance(kinds.get(kind), dict) else {}
    kind_label = spec["label"] if is_text(spec.get("label")) else kind
    meta = ", ".join(part for part in (kind_label, item.get("label")) if is_text(part))
    line = f"{marker} **{one_line(item.get('title')) or '(untitled)'}**"
    if meta:
        line += f" ({meta})"
    if has_pending(item):
        line += " " + _pending_mark(item["pending"])
    lines = [line]
    if is_text(item.get("body")):
        for text in item["body"].strip().split("\n"):
            lines.append(indent + text.rstrip() if text.strip() else "")
    for field in item.get("fields") or []:
        if not (isinstance(field, dict) and is_text(field.get("value"))):
            continue
        values = field["value"].strip().split("\n")
        label = one_line(field.get("label")) or text_of(field.get("key"))
        lines.append(f"{indent}- {label}: {values[0].rstrip()}")
        lines.extend(indent + "  " + extra.rstrip() if extra.strip() else "" for extra in values[1:])
    choices = item.get("choices")
    if isinstance(choices, dict):
        quiz = choices.get("kind") == "quiz"
        prompt = one_line(choices.get("prompt"))
        lines.append(f"{indent}{'Quiz' if quiz else 'Poll'}: {prompt}".rstrip())
        for option in choices.get("options") or []:
            if not isinstance(option, dict):
                continue
            text = f"{indent}- {option.get('id')}. {one_line(option.get('text'))}"
            if quiz and option.get("id") == choices.get("correct"):
                text += " (correct)"
            lines.append(text)
    return lines


def render_markdown(plan):
    """The plan as readable Markdown (PLAN.md)."""
    out = [f"# {one_line(plan.get('title'))}"]
    if is_text(plan.get("summary")):
        out.extend(["", plan["summary"].strip()])
    facts = [fact for fact in plan.get("facts") or [] if isinstance(fact, dict)]
    if facts:
        out.extend(["", "| Fact | Value |", "|---|---|"])
        out.extend(f"| {_cell(fact.get('label'))} | {_cell(fact.get('value'))} |" for fact in facts)
    approved = plan.get("approved")
    if isinstance(approved, dict):
        status = f"Approved {text_of(approved.get('at'))}".rstrip()
        after = approved.get("afterRound", approved.get("round"))
        if is_int(after):
            status += f", after round {after}"
    else:
        status = f"Draft, round {plan.get('round')}"
    out.extend(["", f"**Status:** {status}"])
    if has_pending(plan):
        out.extend(["", f"**Whole plan** {_pending_mark(plan['pending'])}"])
        follow = plan["pending"].get("followUps") if isinstance(plan["pending"], dict) else None
        if is_str_list(follow) and follow:
            out.append("")
            out.extend(f"- {one_line(line)}" for line in follow)
    decisions = [entry for entry in plan.get("decisions") or [] if isinstance(entry, dict)]
    if decisions:
        out.extend(["", "## Decisions", ""])
        for decision in decisions:
            answer = one_line(decision.get("label")) or text_of(decision.get("choice"))
            line = (f"- {one_line(decision.get('question'))}: **{answer}** "
                    f"({decision.get('by', 'owner')}, round {decision.get('round')})")
            if is_text(decision.get("note")):
                line += f". Note: {one_line(decision['note'])}"
            out.append(line)
    kinds = plan.get("itemKinds") if isinstance(plan.get("itemKinds"), dict) else {}
    groups = plan.get("groups") if isinstance(plan.get("groups"), list) else []
    tabs = any(isinstance(group, dict) and group.get("display") == "tab" for group in groups)
    mode = plan.get("numbering")
    if mode not in NUMBERINGS:
        mode = "per-tab" if tabs else "continuous"
    counter = [0]

    def walk(group_list, depth):
        for group in group_list:
            if not isinstance(group, dict):
                continue
            if mode == "per-group" or (mode == "per-tab" and depth == 1):
                counter[0] = 0
            heading = "#" * min(depth + 1, 6)
            mark = " " + _pending_mark(group["pending"]) if has_pending(group) else ""
            out.extend(["", f"{heading} {one_line(group.get('title')) or '(untitled)'}{mark}"])
            if is_text(group.get("summary")):
                out.extend(["", group["summary"].strip()])
            items = [item for item in group.get("items") or [] if isinstance(item, dict)]
            if items:
                out.append("")
            for item in items:
                counter[0] += 1
                out.extend(_item_lines(item, counter[0], mode, kinds))
            walk(group.get("groups") or [], depth + 1)

    walk(groups, 1)
    return "\n".join(out).rstrip() + "\n"


def cmd_render(args):
    paths = Paths(args.plan)
    plan, _counts = checked_plan(args.plan, allow_pending=True)
    out = os.path.abspath(args.out) if args.out else os.path.join(paths.folder, "PLAN.md")
    write_text(out, render_markdown(plan))
    note(f"Wrote {out}")
    return EXIT_OK


def cmd_status(args):
    paths = Paths(args.plan)
    plan, _size = load_plan_text(args.plan)
    if not isinstance(plan, dict):
        raise CprError(f"the plan file is not a JSON object: {args.plan}")
    try:
        review = load_review(paths)
    except CprError as exc:
        note(f"WARNING: {exc.message}")
        review = None
    found = collect_pending(plan)
    items = groups = 0
    for kind, _card, _depth, _parent, _path in walk_cards(plan.get("groups")):
        if kind == "group":
            groups += 1
        else:
            items += 1
    emit({"planId": plan.get("id"), "title": plan.get("title"), "round": plan.get("round"),
          "url": (review or {}).get("url"), "rounds": (review or {}).get("rounds") or [],
          "pending": len(found), "pendingIds": [card_id for _kind, card_id, _t, _p in found],
          "approved": plan.get("approved") if isinstance(plan.get("approved"), dict) else None,
          "items": items, "groups": groups})
    return EXIT_OK


# ---------------------------------------------------------------- command line

class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1, not argparse's 2, which here means a changed, already applied round."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID, f"{self.prog}: error: {message}\n")


def build_parser():
    parser = _Parser(prog="plan_review.py",
                     description="Publish a content plan as a review page, read the owner's "
                                 "review back and apply it to the plan.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    cmd = sub.add_parser("validate", help="check the plan file")
    cmd.add_argument("plan", help="path to plan.json")
    cmd.add_argument("--allow-pending", action="store_true",
                     help="accept cards that still carry pending")
    cmd.add_argument("--json", action="store_true", help="also print the result as JSON")

    cmd = sub.add_parser("build", help="inject the plan into the page template")
    cmd.add_argument("plan", help="path to plan.json")
    cmd.add_argument("--out", required=True, help="where to write the page")
    cmd.add_argument("--template", help="the page template (default: "
                                        "../assets/review-page.template.html beside this script)")
    cmd.add_argument("--standalone", action="store_true",
                     help="wrap the page so it opens from disk")

    cmd = sub.add_parser("published", help="record that the current round was published")
    cmd.add_argument("plan", help="path to plan.json")
    cmd.add_argument("--url", required=True, help="the artifact URL of the page")
    cmd.add_argument("--description", help="the one-sentence description passed to the publish; "
                     "kept in review.json so a later republish can pass the same one")

    cmd = sub.add_parser("apply", help="apply the owner's review to the plan")
    cmd.add_argument("plan", help="path to plan.json")
    cmd.add_argument("--state", required=True,
                     help="the saved review, an export of it, or a folder holding one")
    cmd.add_argument("--allow-unfinished", action="store_true",
                     help="apply although the owner did not press Finish review")
    cmd.add_argument("--reapply", action="store_true",
                     help="start the round over from its saved copy after the review changed")
    cmd.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")

    cmd = sub.add_parser("approve", help="mark the plan approved and write PLAN.md")
    cmd.add_argument("plan", help="path to plan.json")
    cmd.add_argument("--out", help="where to write PLAN.md (default: beside plan.json)")

    cmd = sub.add_parser("render", help="write the plan as readable Markdown")
    cmd.add_argument("plan", help="path to plan.json")
    cmd.add_argument("--out", help="where to write PLAN.md (default: beside plan.json)")

    cmd = sub.add_parser("status", help="where the review stands")
    cmd.add_argument("plan", help="path to plan.json")
    return parser


COMMANDS = {"validate": cmd_validate, "build": cmd_build, "published": cmd_published,
            "apply": cmd_apply, "approve": cmd_approve, "render": cmd_render,
            "status": cmd_status}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except CprError as exc:
        note(f"ERROR: {exc.message}")
        return exc.code
    except OSError as exc:
        note(f"ERROR: {exc}")
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())

"""The `browse` tool: one function, seven actions, and the gate before it matters.

`open`, `observe`, `back`, `forward`, and `scroll` are reads: they change
what is on screen and nothing else, by the same rule that makes `open` a
read. `act` splits by what the DOM says: clicking, typing, choosing, and
pressing an ordinary key are acts, while a control that submits a form --
or an Enter inside one -- is sensitive and asks first, with the control's
label and the host in the read-back, because "Click the button" is not a
question anybody can answer.

`act` takes one reference and lets the page decide which of four things is
meant: ref alone clicks, ref plus text types (or, on a `<select>`, chooses),
ref plus key presses. One verb rather than four, because they all operate
*one control the model just read off `observe`*, and a second verb for each
would be four more choices at the moment the model has the least context.

The gate lives here, in the tool layer, not in `session`: the session module
knows *how* to click and nothing about *whether it may*. That split is what
lets a test patch five functions and prove the refusal order -- password
fields refused outright, changed pages refused, submissions gated, keys
that would submit gated the same way -- without a browser, a network, or a
window.

One tool rather than `web_read` plus a browser tool, because two tools that
both fetch a page is two tool results the model has to reconcile, and one of
them would be the one with the click in it.
"""

from __future__ import annotations

from typing import Any

try:
    from ...runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.permission_gate import require_approval

try:
    from ..tools_compat import RunContext, ToolError, function_tool
    from ..tool_results import as_payload, wrap_untrusted
    from ..tools_text import clip
    from .actions import (
        classify_click,
        classify_key,
        fill_deny_reason,
        hostname_of,
        is_search_control,
        normalize_key,
        normalize_scroll,
        normalize_url,
        observe_payload,
        parse_tab_request,
        TAB_ACTION_CLASS,
        TAB_ASK_VERB,
        LABEL_CHARS,
        MAX_REPORTED_CONTROLS,
        OBSERVE_TEXT_CHARS,
        REF_RE,
        URL_CHARS,
    )
    from .session import (
        MSG_CHANGED,
        MSG_UNKNOWN_REF,
        click_ref,
        close_other_tabs,
        close_tab,
        current_url,
        fill_ref,
        go_history,
        inspect_ref,
        new_tab,
        observe_page,
        open_page,
        press_ref,
        release,
        scroll_page,
        select_ref,
        switch_tab,
        tab_context,
        tab_list,
    )
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import RunContext, ToolError, function_tool
    from tool_results import as_payload, wrap_untrusted
    from tools_text import clip
    from browser.actions import (
        classify_click,
        classify_key,
        fill_deny_reason,
        hostname_of,
        is_search_control,
        normalize_key,
        normalize_scroll,
        normalize_url,
        observe_payload,
        parse_tab_request,
        TAB_ACTION_CLASS,
        TAB_ASK_VERB,
        LABEL_CHARS,
        MAX_REPORTED_CONTROLS,
        OBSERVE_TEXT_CHARS,
        REF_RE,
        URL_CHARS,
    )
    from browser.session import (
        MSG_CHANGED,
        MSG_UNKNOWN_REF,
        click_ref,
        close_other_tabs,
        close_tab,
        current_url,
        fill_ref,
        go_history,
        inspect_ref,
        new_tab,
        observe_page,
        open_page,
        press_ref,
        release,
        scroll_page,
        select_ref,
        switch_tab,
        tab_context,
        tab_list,
    )

#: The surface, kept small on purpose: a tool the model has to choose between
#: too many verbs on is a tool that mis-calls. Eight is the ceiling this set holds
#: to, and each verb below says in its docstring what it needs. An unknown verb is
#: refused by name.
#:
#: `act` is where the sub-modes live, because that is where references live:
#: a click, a fill, a key, and a choice all take the `ref` the model just
#: read off `observe`, and the DOM -- not a second verb to remember --
#: decides which one is meant.
#:
#: `tab` is where the *browser's* sub-modes live, for the same reason `scroll`
#: takes its direction in `text`: opening, listing, switching and closing tabs
#: are one decision about the browser rather than four decisions about controls,
#: and none of them needs a reference.
ACTIONS = ("open", "observe", "act", "back", "forward", "scroll", "tab", "close")

MSG_BAD_ACTION = "Tell me one of: open, observe, act, back, forward, scroll, tab, or close."
MSG_NO_URL = "Tell me which page to open."
MSG_BAD_URL = (
    "I can only open web addresses (http or https) -- I won't open that. "
    "Nothing was shown."
)
MSG_NOT_FILLABLE = "That control doesn't take text -- observe the page again. Nothing was done."
MSG_NOT_VISIBLE = "That control isn't visible right now -- observe the page again. Nothing was done."

#: A reference from a page the user has since navigated away from. Its own
#: sentence, rather than the generic "the page changed", because the recovery is
#: different and specific: the control may well still exist on the *current* page,
#: but this reference does not point at it, and guessing which element now sits at
#: that position is how a click lands on a stranger's chat.
MSG_STALE_REF = (
    "That reference is from a page you've navigated away from, so I won't click it "
    "against this one -- observe the page again and act on what it shows now. "
    "Nothing was clicked."
)
MSG_FILL_PASSWORD = "I won't type into a password field. Nothing was typed."
MSG_ONE_MODIFIER = "Give me either text or a key for an action, not both. Nothing was done."
MSG_BAD_KEY = (
    "I can only press Enter, Escape, Tab, Backspace, Delete, Home, End, PageUp, "
    "PageDown, Space, or an arrow key. Nothing was pressed."
)
MSG_NOT_PRESSABLE = "That control won't take the key right now -- observe the page again. Nothing was pressed."
MSG_NOT_SELECTABLE = "That control doesn't offer choices -- observe the page again. Nothing was done."
MSG_NO_SUCH_OPTION = (
    "That page doesn't offer that choice -- observe it again to see what it does. "
    "Nothing was chosen."
)
MSG_BAD_SCROLL = "Tell me where to scroll: down, up, top, or bottom. Nothing moved."
MSG_BAD_TAB = (
    "I did not understand that. Say 'tab' to list them, 'new' to open one, a tab "
    "number or 'first'/'last'/'next' to go to one, or 'close' (with a number) to "
    "close one. Nothing was done."
)

#: What a `ref` failure turns into, per the reason the page gave.
_CLICK_FAILURES = {
    "gone": MSG_UNKNOWN_REF,
    "changed": MSG_CHANGED,
    "stale": MSG_STALE_REF,
    "not visible": MSG_NOT_VISIBLE,
}
_FILL_FAILURES = {
    "gone": MSG_UNKNOWN_REF,
    "stale": MSG_STALE_REF,
    "password": MSG_FILL_PASSWORD,
    "not fillable": MSG_NOT_FILLABLE,
    "not visible": MSG_NOT_VISIBLE,
}
_PRESS_FAILURES = {
    "gone": MSG_UNKNOWN_REF,
    "changed": MSG_CHANGED,
    "stale": MSG_STALE_REF,
    "not visible": MSG_NOT_VISIBLE,
    "not pressable": MSG_NOT_PRESSABLE,
}
_SELECT_FAILURES = {
    "gone": MSG_UNKNOWN_REF,
    "changed": MSG_CHANGED,
    "stale": MSG_STALE_REF,
    "not visible": MSG_NOT_VISIBLE,
    "not selectable": MSG_NOT_SELECTABLE,
    "no such option": MSG_NO_SUCH_OPTION,
}


def _where_am_i(data: dict) -> str:
    """One sentence saying which tab this is, and what else is open.

    Appended to a read rather than asked for. The report was that Lumine did not
    know which tab she was on or how to get to another one, and the fix that
    works is not a better instruction to the model -- it is that the answer is
    already in every read she takes, so she cannot act on a tab without having
    been told which one it is.

    Kept to one line, and it names the *number*, because that is the argument the
    `tab` verb takes. A list of titles without their numbers would be a fact she
    could not act on.
    """
    if not isinstance(data, dict) or data.get("tab") is None:
        return ""
    which = data.get("tab")
    others = [row for row in data.get("otherTabs") or [] if isinstance(row, dict)]
    said = f" (tab {which} of {data.get('tabCount')}"
    if others:
        names = ", ".join(
            f"tab {row.get('tab')}: {clip(str(row.get('title') or ''), 40)}" for row in others
        )
        said += f"; also open -- {names}"
    return said + ")"


def _failure(mapping: dict[str, str], reason: object) -> str:
    # An unknown reason is treated as the worst one we know: a sentence that
    # says nothing happened beats one that claims something did.
    return mapping.get(str(reason or ""), MSG_CHANGED)


def _click_report(label: str, on_host: str, outcome: dict) -> str:
    """Report a click as what it *did*, not as what was attempted.

    The bare "Clicked X." was a false success, and it was expensive: clicking a
    video result on YouTube and landing on the home page was reported as a
    click, so the model carried on and answered from whatever text was left on
    screen. Two facts are now stated instead, because a person repeating them to
    Lumine would say both:

    * **where** the page went, when it went anywhere -- so "played the video" and
      "went to the home page" cannot be confused;
    * **that nothing moved**, when it did not, which is the observation a
      single-page app cannot distinguish from a slow load and the one that stops
      the model claiming a result it did not see.

    No navigation on a page that was already moving is not treated as failure:
    plenty of controls expand a menu, start a timer, or load more rows, and
    calling those failures would train the model to distrust working clicks.
    """
    after = outcome.get("after") or {}
    landed = str(after.get("url") or "").strip()
    if not landed:
        return f"Clicked {label}{on_host}."
    if outcome.get("moved"):
        return f"Clicked {label}{on_host} -- the page is now at {clip(landed, URL_CHARS)}."
    return (
        f"Clicked {label}{on_host}, but the page stayed on "
        f"{clip(landed, URL_CHARS)}. The click was delivered; read the page again "
        f"before you rely on it."
    )


#: ``_CLICK_FAILURES`` etc. read a reason from the page; the reason a key was
#: classified ``sensitive`` is a DOM fact, and this is the one place it is
#: resolved for both element actions.
def _is_search(info: Any) -> bool:
    """Whether ``inspect_ref``'s facts say this control is a search box.

    A thin reader over :func:`actions.is_search_control`, so the two browser
    tools agree by construction rather than by two copies of the same rule. The
    facts come from ``session._JS_FACTS``, which reads ``role``, ``formRole``
    and ``soleField`` alongside the ``tag``/``type``/``inForm`` the freshness
    signature already covered.
    """
    return is_search_control(
        tag=info.get("tag"),
        type_=info.get("type"),
        role=info.get("role"),
        in_form=bool(info.get("inForm")),
        form_role=info.get("formRole"),
        sole_field=bool(info.get("soleField")),
    )


@function_tool()
async def browse(
    context: RunContext,
    action: str,
    url: str = "",
    ref: str = "",
    text: str = "",
    key: str = "",
) -> str:
    """Browse a page in Lumine's own isolated browser window.

    One tool, eight actions:
    - open: go to a page (needs url; http/https only).
    - observe: read the current tab -- title, address, a short excerpt, and
      the visible controls with references like "r3".
    - act: operate one control you named by reference. Give ref alone to
      click it; ref and text to type into it, or -- on a dropdown -- to
      choose its option; ref and key to press Enter, Escape, or an arrow key
      on it. Submitting a form always asks the user first, whether by click
      or by Enter. Password fields are refused -- Lumine never types
      passwords.
    - back / forward: one step through this tab's own history.
    - scroll: move the view -- text "down" (the default), "up", "top", or
      "bottom".
    - tab: work with more than one tab. Say nothing to list them; "new" to
      open another (add a url to open a page in it), a tab's number to switch
      to it, or "close" (with or without a number) to close one. Tabs are how
      you keep a search results page while you follow a link, or compare two
      pages -- a link that opens its own window now becomes a tab instead of
      replacing the one you were on.
    - close: close the browser and every tab in it.

    **"open" reuses the tab in use; "tab new" adds one.** If the user wants to
    keep what is on screen -- "open this in a new tab", "check that without
    losing this" -- use "tab new" with the url. "open" navigates the current tab,
    which throws the page away, and the result says so when other tabs exist.

    Every read says which tab it read ("tab 2 of 3; also open -- tab 1: ..."),
    so the number is never something to remember. References come from observe
    and go stale after any act or navigation, and after switching tabs -- each
    tab mints its own, so read the tab you are about to act on. Private
    addresses (localhost, your home network's devices) are never opened. Prefer
    search_web for a plain question; this is for a page that must be looked at
    or operated.

    Args:
        action: One of "open", "observe", "act", "back", "forward", "scroll",
            "tab", "close".
        url: The page to open, for action "open", or for action "tab" with
            "new".
        ref: A control reference from observe, for action "act".
        text: The text to type for action "act", where to scroll for action
            "scroll", or which tab request for action "tab".
        key: The key to press, for action "act" with a ref.
    """
    normalized = str(action or "").strip().lower()
    if normalized not in ACTIONS:
        raise ToolError(MSG_BAD_ACTION)

    # -- open ---------------------------------------------------------------
    if normalized == "open":
        raw = str(url or "").strip()
        if not raw:
            raise ToolError(MSG_NO_URL)
        try:
            target = normalize_url(raw)
        except ValueError:
            raise ToolError(MSG_BAD_URL) from None
        # Already on it? Do not navigate -- report what is there instead.
        #
        # This is the fix for "she opened YouTube three times in one turn". Every
        # re-open discarded the results page the previous call had just read, so
        # the model started over each time and never got further. Answering with
        # the page's own contents costs one call and ends the loop, where a
        # refusal would have been a dead end and a silent success would have
        # taught the model nothing.
        try:
            here = str(await current_url() or "")
        except Exception:  # noqa: BLE001 - the shortcut is never worth failing an open
            here = ""
        if here and here.rstrip("/") == target.rstrip("/") and here != "about:blank":
            payload: dict = {
                "alreadyOpen": True,
                "title": "",
                "url": clip(here, 90),
                "notice": (
                    "That page is already open. I did not open it again. "
                    "Use observe to read it or scroll to move on it."
                ),
            }
            try:
                data = await observe_page()
                payload["title"] = clip(str(data.get("title") or ""), 70)
                payload["text"] = clip(str(data.get("text") or ""), OBSERVE_TEXT_CHARS)
                payload["controls"] = [
                    {"ref": c.get("ref"), "kind": c.get("tag"), "label": clip(str(c.get("label") or ""), LABEL_CHARS)}
                    for c in list(data.get("controls") or [])[:MAX_REPORTED_CONTROLS]
                    if isinstance(c, dict) and REF_RE.match(str(c.get("ref") or ""))
                ][:MAX_REPORTED_CONTROLS]
            except ToolError:
                # `observe` refuses on about:blank; here it means the page went
                # somewhere on its own. The notice still stands.
                pass
            return wrap_untrusted(as_payload(payload))
        # A read, so the default policy answers immediately -- but the gate
        # is still the door, so `browse=off` in settings actually closes it.
        await require_approval(
            context,
            tool="browse",
            action="open",
            action_class="read",
            summary=f"Open {hostname_of(target)}",
            target=clip(target, 100),
        )
        page = await open_page(target)
        # Wrapped: the title is text a website chose, and the model reads it.
        # `notice` rides along because the page itself cannot carry it -- an
        # address that is not a video loads successfully and looks like a fine
        # result, so without a sentence saying so the model reports a working
        # page and the user goes looking for a YouTube fault that is not there.
        payload: dict = {"title": clip(page["title"], 70), "url": clip(page["url"], 90)}
        notice = page.get("notice")
        if notice:
            payload["notice"] = str(notice)
        # `open` reuses the tab in use, which is right most of the time and is
        # also how a page gets thrown away. Saying so is what lets the model
        # notice it did it: the alternative is `tab new`, and a model that
        # believes it just opened a second tab will not go looking for the first.
        where = await tab_context()
        if isinstance(where, dict) and where.get("tab") is not None:
            payload["tab"] = where["tab"]
            payload["tabCount"] = where["tabCount"]
            if where["tabCount"] > 1:
                payload["notice"] = (
                    f"This replaced the page in tab {where['tab']}; the other "
                    f"{where['tabCount'] - 1} tab(s) are untouched. Use 'tab new' "
                    "next time to keep this one."
                )
        return wrap_untrusted(as_payload(payload))

    # -- observe ------------------------------------------------------------
    if normalized == "observe":
        await require_approval(
            context,
            tool="browse",
            action="observe",
            action_class="read",
            summary="Read the current page",
        )
        data = await observe_page()
        return wrap_untrusted(
            observe_payload(
                title=data.get("title", ""),
                url=data.get("url", ""),
                text=data.get("text", ""),
                controls=data.get("controls") or [],
            )
        ) + _where_am_i(data)

    # -- back / forward -----------------------------------------------------
    #
    # Reads, by the rule that already makes `open` a read: the tab shows a
    # page it has already been to, and nothing leaves the machine to do it.
    if normalized in ("back", "forward"):
        await require_approval(
            context,
            tool="browse",
            action=normalized,
            action_class="read",
            summary=f"Go {normalized} in the browser",
        )
        data = await go_history(normalized)
        if not data.get("moved"):
            # Playwright's `null` covers both "no earlier page" and "a
            # navigation that was cancelled", and we cannot tell which, so
            # the sentence does not commit to one.
            return f"The browser did not go {normalized} -- there may be no earlier page."
        where = clip(str(data.get("title") or data.get("url") or ""), 70)
        return f"Went {normalized} to {where}."

    # -- scroll -------------------------------------------------------------
    if normalized == "scroll":
        try:
            direction = normalize_scroll(text)
        except ValueError:
            raise ToolError(MSG_BAD_SCROLL) from None
        await require_approval(
            context,
            tool="browse",
            action="scroll",
            action_class="read",
            summary=f"Scroll the page {direction}",
        )
        data = await scroll_page(direction)
        if not data.get("scrolled"):
            # Honest about a page that has nothing below it: "scrolled" about
            # a page that never moved would be a claim with nothing behind it.
            return "That page fits on screen -- there is nothing to scroll."
        landed = int(data.get("y") or 0)
        limit = int(data.get("max") or 0)
        if direction == "top":
            return "Scrolled to the top of the page."
        if direction == "bottom":
            return "Scrolled to the bottom of the page."
        if direction == "up":
            return "Scrolled up." + (" That is the top of the page." if landed <= 0 else "")
        return "Scrolled down." + (" That is the bottom of the page." if landed >= limit else "")

    # -- tab ----------------------------------------------------------------
    #
    # Four operations behind one verb, chosen by `text` the way `scroll` chooses
    # its direction. A tab is the *browser's* state rather than a control on a
    # page, so none of these needs a reference, and splitting them into four
    # verbs would have put four more choices in front of the model at the moment
    # it has the least context.
    if normalized == "tab":
        try:
            op, arg = parse_tab_request(text)
        except ValueError:
            raise ToolError(MSG_BAD_TAB) from None

        # The class is the operation's, not the verb's -- `tab` is one verb that
        # reads three ways and asks once. See `TAB_ACTION_CLASS`.
        action_class = TAB_ACTION_CLASS[op]

        if op == "list":
            await require_approval(
                context,
                tool="browse",
                action=TAB_ASK_VERB,
                action_class=action_class,
                summary="List the browser tabs",
            )
            data = await tab_list()
            rows = [row for row in data.get("tabs") or [] if isinstance(row, dict)]
            if not rows:
                return "There is one tab open and it is blank."
            lines = []
            for row in rows:
                mark = " (the one you're on)" if row.get("active") else ""
                where = str(row.get("title") or row.get("url") or "a blank page")
                lines.append(f"tab {row.get('tab')}: {clip(where, 60)}{mark}")
            listed = "; ".join(lines)
            # Sentence first, payload after it: the delimiter marks where
            # retrieved data begins, so prose in front of it stays prose.
            return f"Open tabs -- {listed}. " + wrap_untrusted(
                as_payload(
                    {
                        "tabs": [
                            {
                                "tab": row.get("tab"),
                                "title": clip(str(row.get("title") or ""), 70),
                                "url": clip(str(row.get("url") or ""), URL_CHARS),
                                "active": bool(row.get("active")),
                            }
                            for row in rows
                        ],
                        "active": data.get("active"),
                    }
                )
            )

        if op == "new":
            address = arg or url
            await require_approval(
                context,
                tool="browse",
                action=TAB_ASK_VERB,
                action_class=action_class,
                summary="Open a new browser tab",
                target=clip(address, 100),
            )
            data = await new_tab(address)
            key = data.get("tab")
            where = clip(str(data.get("title") or data.get("url") or ""), 70)
            count = int(data.get("count") or 1)
            said = f"Opened tab {key}"
            if where and where != "about:blank":
                said += f" on {where}"
            else:
                said += ". It is blank"
            said += f". There are {count} tab{'s' if count != 1 else ''} open."
            return said

        if op == "switch":
            await require_approval(
                context,
                tool="browse",
                action=TAB_ASK_VERB,
                action_class=action_class,
                summary=f"Switch to browser tab {arg}" if arg else "Switch browser tab",
                target=clip(arg, 20),
            )
            data = await switch_tab(arg)
            where = clip(str(data.get("title") or data.get("url") or ""), 70)
            if not where or where == "about:blank":
                where = ""
            # Named, because the model has to read before it acts: a reference it
            # minted on the tab it just left says nothing about this one, and the
            # next action is refused anyway -- better to say so here than to have
            # it discover that as an error.
            return (
                f"On tab {data.get('tab')} now{': ' + where if where else ''}. "
                "Read it before acting -- earlier references belong to the tab you left."
            )

        if op == "close_others":
            await require_approval(
                context,
                tool="browse",
                action=TAB_ASK_VERB,
                action_class=action_class,
                summary="Close every browser tab except this one",
            )
            data = await close_other_tabs()
            gone = data.get("closed") or []
            count = int(data.get("count") or 0)
            return (
                f"Closed {len(gone) if isinstance(gone, list) else 1} other tab(s). "
                f"{count} left, and it is the one you were on."
            )

        # op == "close"
        await require_approval(
            context,
            tool="browse",
            action=TAB_ASK_VERB,
            action_class=action_class,
            summary=f"Close browser tab {arg}" if arg else "Close the current browser tab",
            target=clip(arg, 20),
        )
        data = await close_tab(arg)
        count = int(data.get("count") or 0)
        if not count:
            return (
                f"Closed tab {data.get('closed')}. That was the last one -- "
                "a blank tab is open now."
            )
        remaining = [row for row in data.get("tabs") or [] if isinstance(row, dict)]
        on = next(
            (str(row.get("title") or row.get("url") or "") for row in remaining if row.get("active")),
            "",
        )
        said = f"Closed tab {data.get('closed')}. {count} tab{'s' if count != 1 else ''} open"
        if on:
            said += f", on {clip(on, 50)} now"
        return said + "."

    # -- close --------------------------------------------------------------
    if normalized == "close":
        await require_approval(
            context,
            tool="browse",
            action="close",
            action_class="act",
            summary="Close the browser",
        )
        await release()
        return "The browser is closed."

    # -- act ----------------------------------------------------------------
    chosen = str(ref or "").strip()
    if not REF_RE.match(chosen):
        raise ToolError(MSG_UNKNOWN_REF)

    # Two modifiers are two different requests; guessing which was meant is
    # how a key gets pressed in a field that was meant to receive text.
    typed = str(text or "")
    pressed = str(key or "")
    if typed.strip() and pressed.strip():
        raise ToolError(MSG_ONE_MODIFIER)

    # A key is checked against the closed list before the page is touched at
    # all: a key we will not press must be reported as refused, never
    # discovered *after* a round trip as some other key that went through.
    which = None
    if pressed.strip():
        try:
            which = normalize_key(pressed)
        except ValueError:
            raise ToolError(MSG_BAD_KEY) from None

    # Facts first, then the question, then -- and only then -- the action.
    # The click and the key re-check those facts at the moment they act, so a
    # page cannot swap elements between our two steps.
    info = await inspect_ref(chosen)
    if not info.get("found"):
        raise ToolError(MSG_UNKNOWN_REF)
    label = clip(str(info.get("label") or info.get("tag") or "control"), 40)
    host = hostname_of(info.get("url", ""))
    on_host = f" on {host}" if host else ""
    target = clip(str(info.get("url", "")), 100)
    signature = str(info.get("sig") or "")

    if which is not None:
        # -- act: press a key on it ----------------------------------------
        # Enter in a form is a submission and says so, except in a search form,
        # where submitting is searching; a key that activates the control is
        # classified as the click it is; everything else only moves focus or
        # the caret. `is_search_control` and `web_control._classify` compute
        # this from the same facts, so one search cannot be priced two ways.
        action_class = classify_key(
            which,
            tag=info.get("tag"),
            type_=info.get("type"),
            in_form=bool(info.get("inForm")),
            search=_is_search(info),
        )
        shown = "Space" if which == " " else which
        await require_approval(
            context,
            tool="browse",
            action="press",
            action_class=action_class,
            summary=f"Press {shown} on {label}{on_host}",
            target=target,
        )
        outcome = await press_ref(chosen, signature, which)
        if not outcome.get("ok"):
            raise ToolError(_failure(_PRESS_FAILURES, outcome.get("reason")))
        return f"Pressed {shown} on {label}{on_host}."

    if typed.strip():
        # The refusal, before any question: a card the user answers "yes" to
        # is not consent to have their password typed anywhere. This repeats
        # the check `session` makes inside the DOM, on purpose.
        reason = fill_deny_reason(tag=info.get("tag"), type_=info.get("type"))
        if reason:
            raise ToolError(MSG_FILL_PASSWORD)

        if str(info.get("tag") or "").casefold() == "select":
            # -- act: choose an option ------------------------------------
            # The DOM decides this, not the argument: a dropdown given text
            # is being asked for a choice, and a text field given text is
            # being asked to type. An `act` on a select with no text would
            # be a click that only opens the list, which is why the two are
            # apart rather than one verb with a flag.
            wanted = clip(typed, 120)
            await require_approval(
                context,
                tool="browse",
                action="select",
                action_class="act",
                summary=f"Choose {wanted} in {label}{on_host}",
                target=target,
            )
            outcome = await select_ref(chosen, signature, wanted)
            if not outcome.get("ok"):
                raise ToolError(_failure(_SELECT_FAILURES, outcome.get("reason")))
            picked = clip(str(outcome.get("chosen") or wanted), 60)
            return f"Chose {picked} in {label}{on_host}."

        # -- act: type into it ---------------------------------------------
        await require_approval(
            context,
            tool="browse",
            action="fill",
            action_class="act",
            summary=f"Type into {label}{on_host}",
            target=target,
        )
        outcome = await fill_ref(chosen, clip(typed, 600))
        if not outcome.get("ok"):
            raise ToolError(_failure(_FILL_FAILURES, outcome.get("reason")))
        return f"Typed into {label}{on_host}."

    # -- act: click it ------------------------------------------------------
    action_class = classify_click(
        tag=info.get("tag"),
        type_=info.get("type"),
        in_form=bool(info.get("inForm")),
        search=_is_search(info),
    )
    await require_approval(
        context,
        tool="browse",
        action="click",
        action_class=action_class,
        summary=f"Click {label}{on_host}",
        target=target,
    )
    outcome = await click_ref(chosen, signature)
    if not outcome.get("ok"):
        raise ToolError(_failure(_CLICK_FAILURES, outcome.get("reason")))
    return _click_report(label, on_host, outcome)
